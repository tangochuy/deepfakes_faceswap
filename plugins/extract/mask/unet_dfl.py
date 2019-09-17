#!/us#!/usr/bin/env python3

import cv2
import keras
import numpy as np
from ._base import Masker, logger


class Mask(Masker):
    """ Perform transformation to align and get landmarks """
    def __init__(self, **kwargs):
        git_model_id = 6
        model_filename = "DFL_256_sigmoid_v1.h5"
        super().__init__(git_model_id=git_model_id,
                         model_filename=model_filename,
                         **kwargs)
        self.vram = 3440
        self.min_vram = 1024  # TODO determine
        self.mask_input_size = 256
        self.model = None
        self.supports_plaidml = True

    def initialize(self, *args, **kwargs):
        """ Initialization tasks to run prior to alignments """
        try:
            super().initialize(*args, **kwargs)
            logger.info("Initializing U-Net Mask Network(256)...")
            logger.debug("U-Net initialize: (args: %s kwargs: %s)", args, kwargs)
            self.configure_session()
            self.init.set()
            logger.info("Initialized U-Net Mask Network(256)")
        except Exception as err:
            self.error.set()
            raise err

    def load_model(self):
        model = keras.models.load_model(self.model_path)
        return model

    # MASK PROCESSING
    def build_masks(self, image, detected_face, input_size, output_size, coverage_ratio):
        """ Function for creating facehull masks
            Faces may be of shape (batch_size, height, width, 3) or (height, width, 3)
        """
        # pylint: disable=no-member
        postprocess_test = False
        image = np.array(image)
        detected_face.load_aligned(image, size=self.mask_input_size, align_eyes=False, dtype='float32')
        feed_face = detected_face.aligned["face"]
        mask = np.zeros(feed_face.shape[:-1] + (1, ), dtype='float32')
        model_input = feed_face / 255.
        
        results = self.model.predict_on_batch(model_input[None, :, :, :3])
        generator = (cv2.GaussianBlur(mask, (7, 7), 0) for mask in results)
        if postprocess_test:
            generator = (self.postprocessing(mask[..., None]) for mask in results)
        results = np.array(tuple(generator))[..., None]
        results[results < 0.05] = 0.
        results[results > 0.95] = 1.
        results *= 255.

        detected_face.load_feed_face(image,
                                     size=input_size,
                                     coverage_ratio=coverage_ratio)
        feed_face = detected_face.feed["face"]
        feed_mask = self.resize(results, input_size).astype('uint8')
        feed_mask = np.squeeze(feed_mask, axis=0)
        feed_img = np.concatenate((feed_face[..., :3], feed_mask), axis=-1)
        detected_face.feed["face"] = feed_img

        if input_size != output_size:
            detected_face.load_reference_face(image,
                                         size=output_size,
                                         coverage_ratio=coverage_ratio)
            ref_face = detected_face.reference["face"]
            ref_mask = self.resize(results, output_size).astype('uint8')
            ref_mask = np.squeeze(ref_mask, axis=0)
            ref_img = np.concatenate((ref_face[..., :3], ref_mask), axis=-1)
            detected_face.reference["face"] = ref_img
        return detected_face
