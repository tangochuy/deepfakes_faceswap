#!/usr/bin/env python3
""" Base class for Face Masker plugins
    Plugins should inherit from this class

    See the override methods for which methods are required.

    The plugin will receive a dict containing:
    {"filename": <filename of source frame>,
     "image": <source image>,
     "face_bounding_boxes": <list of bounding box dicts from lib/plugins/extract/detect/_base>}

    For each source item, the plugin must pass a dict to finalize containing:
    {"filename": <filename of source frame>,
     "image": <four channel source image>,
     "face_bounding_boxes": <list of bounding box dicts from lib/plugins/extract/detect/_base>,
     "mask": <one channel mask image>}
    """

import logging
import os
import traceback
import cv2
import numpy as np
import keras

from io import StringIO
from lib.faces_detect import DetectedFace
from lib.aligner import Extract
from lib.gpu_stats import GPUStats
from lib.utils import GetModel

logger = logging.getLogger(__name__)  # pylint:disable=invalid-name


class Masker():
    """ Face Mask Object
    Faces may be of shape (batch_size, height, width, 3) or (height, width, 3)
        of dtype unit8 and with range[0, 255]
        Landmarks may be of shape (batch_size, 68, 2) or (68, 2)
        Produced mask will be in range [0, 255]
        channels: 1, 3 or 4:
                    1 - Returns a single channel mask
                    3 - Returns a 3 channel mask
                    4 - Returns the original image with the mask in the alpha channel """
    def __init__(self, loglevel='VERBOSE', configfile=None, input_size=256, output_size=256,
                 coverage_ratio=1., git_model_id=None, model_filename=None):
        logger.debug("Initializing %s: (loglevel: %s, configfile: %s, input_size: %s, "
                     "output_size: %s, coverage_ratio: %s,git_model_id: %s, model_filename: '%s')",
                     self.__class__.__name__, loglevel, configfile, input_size, output_size,
                     coverage_ratio, git_model_id, model_filename)
        self.loglevel = loglevel
        self.input_size = input_size
        self.output_size = output_size
        self.coverage_ratio = coverage_ratio
        self.extract = Extract()
        self.parent_is_pool = False
        self.init = None
        self.error = None

        # The input and output queues for the plugin.
        # See lib.queue_manager.QueueManager for getting queues
        self.queues = {"in": None, "out": None}

        #  Get model if required
        self.model_path = self.get_model(git_model_id, model_filename)

        # Approximate VRAM required for masker. Used to calculate
        # how many parallel processes / batches can be run.
        # Be conservative to avoid OOM.
        self.vram = None

        # Set to true if the plugin supports PlaidML
        self.supports_plaidml = False

        logger.debug("Initialized %s", self.__class__.__name__)

    # <<< OVERRIDE METHODS >>> #
    # These methods must be overriden when creating a plugin
    def initialize(self, *args, **kwargs):
        """ Inititalize the masker
            Tasks to be run before any masking is performed.
            Override for specific masker """
        logger.debug("_base initialize %s: (PID: %s, args: %s, kwargs: %s)",
                     self.__class__.__name__, os.getpid(), args, kwargs)
        self.init = kwargs["event"]
        self.error = kwargs["error"]
        self.queues["in"] = kwargs["in_queue"]
        self.queues["out"] = kwargs["out_queue"]

    def configure_session(self):
        """ Set the TF Session and initialize """
        # Must import tensorflow inside the spawned process
        # for Windows machines
        global tf  # pylint: disable = invalid-name,global-statement
        import tensorflow as tflow
        tf = tflow

        card_id, vram_free, vram_total = self.get_vram_free()
        vram_ratio = 1. if vram_free <= self.vram else self.vram / vram_total
        config = tf.ConfigProto()
        if card_id != -1:
            config.gpu_options.visible_device_list = str(card_id)
        config.gpu_options.per_process_gpu_memory_fraction = vram_ratio

        self.mask_session = tf.Session(config=config)
        self.mask_graph = tf.get_default_graph()
        keras.backend.set_session(self.mask_session)

        with self.mask_graph.as_default():
            with self.mask_session.as_default():
                if any("gpu" in str(device).lower() for device in self.mask_session.list_devices()):
                    logger.debug("Using GPU")
                    # self.batch_size = int(alloc / self.vram)
                else:
                    logger.warning("Using CPU")
                    # self.batch_size = int(alloc / self.vram)
            self.model = self.load_model()
            self.model._make_predict_function()

    def build_masks(self, faces, means, landmarks):
        """ Override to build the mask """
        raise NotImplementedError

    # <<< GET MODEL >>> #
    @staticmethod
    def get_model(git_model_id, model_filename):
        """ Check if model is available, if not, download and unzip it """
        if model_filename is None:
            logger.debug("No model_filename specified. Returning None")
            return None
        if git_model_id is None:
            logger.debug("No git_model_id specified. Returning None")
            return None
        cache_path = os.path.join(os.path.dirname(__file__), ".cache")
        model = GetModel(model_filename, cache_path, git_model_id)
        return model.model_path

    # <<< MASKING WRAPPER >>> #
    def run(self, *args, **kwargs):
        """ Parent align process.
            This should always be called as the entry point so exceptions
            are passed back to parent.
            Do not override """
        try:
            self.mask(*args, **kwargs)
        except Exception:  # pylint:disable=broad-except
            logger.error("Caught exception in child process: %s", os.getpid())
            # Display traceback if in initialization stage
            if not self.init.is_set():
                logger.exception("Traceback:")
            tb_buffer = StringIO()
            traceback.print_exc(file=tb_buffer)
            exception = {"exception": (os.getpid(), tb_buffer)}
            self.queues["out"].put(exception)
            exit(1)

    def mask(self, *args, **kwargs):
        """ Process masks """
        if not self.init:
            self.initialize(*args, **kwargs)
        logger.debug("Launching Mask: (args: %s kwargs: %s)", args, kwargs)

        for item in self.get_item():
            if item == "EOF":
                self.finalize(item)
                break

            logger.trace("Masking faces")
            try:
                item["masked_faces"] = self.process_masks(item["image"],
                                                          item["filename"],
                                                          item["landmarks"],
                                                          item["face_bounding_boxes"],
                                                          input_size = self.input_size,
                                                          output_size = self.output_size,
                                                          coverage_ratio = self.coverage_ratio)
                logger.trace("Masked faces: %s", item["filename"])
            except ValueError as err:
                logger.warning("Image '%s' could not be processed. This may be due to corrupted "
                               "data: %s", item["filename"], str(err))
                item["face_bounding_boxes"] = list()
                item["masked_faces"] = list()
                # UNCOMMENT THIS CODE BLOCK TO PRINT TRACEBACK ERRORS
                import sys
                exc_info = sys.exc_info()
                traceback.print_exception(*exc_info)
            self.finalize(item)
        logger.debug("Completed Mask")

    def process_masks(self, image, filename, landmarks, face_bounding_boxes, input_size, output_size, coverage_ratio):
        """ Align image and process landmarks """
        logger.trace("Processing masks")
        retval = list()
        for face_box, landmark in zip(face_bounding_boxes, landmarks):
            detected_face = DetectedFace(landmarksXY=landmark, filename=filename)
            detected_face.from_bounding_box_dict(face_box, image)
            detected_face = self.build_masks(image, detected_face, input_size, output_size, coverage_ratio)
            retval.append(detected_face)
        logger.trace("Processed masks")
        return retval

    @staticmethod
    def resize(image, target_size):
        """ resize input and output of mask models appropriately """
        _, height, width, channels = image.shape
        image_size = max(height, width)
        scale = target_size / image_size
        if scale == 1.:
            return image
        method = cv2.INTER_CUBIC if scale > 1. else cv2.INTER_AREA  # pylint: disable=no-member
        generator = (cv2.resize(img,  # pylint: disable=no-member
                                (0, 0), fx=scale, fy=scale, interpolation=method) for img in image)
        resized = np.array(tuple(generator))
        resized = resized if channels > 1 else resized[..., None]
        return resized 

    # <<< FINALIZE METHODS >>> #
    def finalize(self, output):
        """ This should be called as the final task of each plugin
            aligns faces and puts to the out queue """
        if output == "EOF":
            logger.trace("Item out: %s", output)
            self.queues["out"].put("EOF")
            return
        logger.trace("Item out: %s", {key: val
                                      for key, val in output.items()
                                      if key != "image"})
        self.queues["out"].put((output))

    # <<< MISC METHODS >>> #
    def get_vram_free(self):
        """ Return free and total VRAM on card with most VRAM free"""
        stats = GPUStats()
        vram = stats.get_card_most_free(supports_plaidml=self.supports_plaidml)
        logger.verbose("Using device %s with %sMB free of %sMB",
                       vram["device"],
                       int(vram["free"]),
                       int(vram["total"]))
        return int(vram["card_id"]), int(vram["free"]), int(vram["total"])

    def get_item(self):
        """ Yield one item from the queue """
        while True:
            item = self.queues["in"].get()
            if isinstance(item, dict):
                logger.trace("Item in: %s", {key: val
                                             for key, val in item.items()
                                             if key != "image"})
                # Pass Detector failures straight out and quit
                if item.get("exception", None):
                    self.queues["out"].put(item)
                    exit(1)
            else:
                logger.trace("Item in: %s", item)
            yield item
            if item == "EOF":
                break

    @staticmethod
    def postprocessing(mask):
        """ Post-processing of Nirkin style segmentation masks """
        # pylint: disable=no-member
        # Select_largest_segment
        pop_small_segments = False  # Don't do this right now
        if pop_small_segments:
            results = cv2.connectedComponentsWithStats(mask,  # pylint: disable=no-member
                                                       4,
                                                       cv2.CV_32S)  # pylint: disable=no-member
            _, labels, stats, _ = results
            segments_ranked_by_area = np.argsort(stats[:, -1])[::-1]
            mask[labels != segments_ranked_by_area[0, 0]] = 0.

        # Smooth contours
        smooth_contours = False  # Don't do this right now
        if smooth_contours:
            iters = 2
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT,  # pylint: disable=no-member
                                               (5, 5))
            cv2.morphologyEx(mask, cv2.MORPH_OPEN,  # pylint: disable=no-member
                             kernel, iterations=iters)
            cv2.morphologyEx(mask, cv2.MORPH_CLOSE,  # pylint: disable=no-member
                             kernel, iterations=iters)
            cv2.morphologyEx(mask, cv2.MORPH_CLOSE,  # pylint: disable=no-member
                             kernel, iterations=iters)
            cv2.morphologyEx(mask, cv2.MORPH_OPEN,  # pylint: disable=no-member
                             kernel, iterations=iters)

        # Fill holes
        fill_holes = True
        if fill_holes:
            not_holes = mask.copy()
            not_holes = np.pad(not_holes, ((2, 2), (2, 2), (0, 0)), 'constant')
            cv2.floodFill(not_holes, None, (0, 0), 255)  # pylint: disable=no-member
            holes = cv2.bitwise_not(not_holes)[2:-2, 2:-2]  # pylint: disable=no-member
            mask = cv2.bitwise_or(mask, holes)  # pylint: disable=no-member
            mask = np.expand_dims(mask, axis=-1)
        return mask
