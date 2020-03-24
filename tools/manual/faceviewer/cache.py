#!/usr/bin/env python3
""" Media objects for the manual adjustments tool """
import logging
import os
import tkinter as tk
from concurrent import futures
from time import sleep

import cv2
import imageio
import numpy as np

from lib.image import SingleFrameLoader

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class FaceCache():
    """ Holds the :class:`tkinter.PhotoImage` faces and their associated mesh landmarks for each
    face in the current input for display in the :class:`~tools.manual.manual.FaceViewer` canvas.

    Parameters
    ----------
    canvas: :class:`tkinter.Canvas`
        The :class:`~tools.manual.FacesViewer` canvas
    scaling_factor: float
        The scaling factor, based on DPI, to apply to the stored objects
    detected_faces: :class:`~tool.manual.faces.DetectedFaces`
        The :class:`~lib.faces_detect.DetectedFace` objects for this video
    tk_face_loading: :class:`tkinter.BooleanVar`
        Variable to indicate whether faces are currently loading into the cache or not
    """
    def __init__(self, canvas, scaling_factor, detected_faces, tk_face_loading):
        logger.debug("Initializing %s: (canvas: %s, scaling_factor: %s, detected_faces: %s, "
                     "tk_face_loading: %s)", self.__class__.__name__, canvas, scaling_factor,
                     detected_faces, tk_face_loading)
        self._frames = canvas._frames
        self._face_size = int(round(96 * scaling_factor))
        self._canvas = canvas
        self._tk_loading = tk_face_loading

        self._loader = FaceCacheLoader(self, detected_faces)
        self._mask_loader = MaskLoader(self, detected_faces)

        self._tk_faces = np.array([None for _ in range(self._frames.frame_count)])
        self._initialized = False
        logger.debug("Initialized %s", self.__class__.__name__)

    # << STANDARD PROPERTIES >> #
    @property
    def tk_faces(self):
        """ list: Item for each frame containing a list of :class:`TKFace` objects for each face in
        each frame. """
        return self._tk_faces

    @property
    def size(self):
        """ int: The size of each individual face in pixels. """
        return self._face_size

    # << STATUS PROPERTIES >> #
    @property
    def is_loading(self):
        """ bool: ``True`` if the faces are currently being updated otherwise ``False``. """
        return self._tk_loading.get()

    @property
    def is_initialized(self):
        """ bool: ``True`` if the faces have completed initial loading otherwise ``False``. """
        return self._initialized

    # << UTILITY FUNCTION PROPERTIES >> #
    @property
    def mask_loader(self):
        """ :class:`MaskLoader`: Handles the application of the selected mask on to each
        :class:`tkinter.PhotoImage`. """
        return self._mask_loader

    @property
    def loader(self):
        """ :class:`FacesCacheLoader: Handles the initial loading of faces and mesh landmarks
        into the :class:`FacesCache`. """
        return self._loader

    def set_initialized(self):
        """ Set the :attr:`_initialized` to ``True``. Called from :class:`FacesCacheLoader` when
        faces have completed loading. """
        self._initialized = True

    def add(self, frame_index, face, landmarks, mask=None):
        """ Add a new :class:`TKFace` object to the :attr:`tk_faces` for the given frame index.

        Parameters
        ----------
        frame_index: int
            The frame index that the given face is to be added to
        face: :class:`numpy.ndarray`
            The face, sized correctly, to be added to :attr:`tk_faces`
        landmarks: :class:`numpy.ndarray`
            The 68 point aligned landmarks for the given face
        mask: :class:`numpy.ndarray` or ``None``, optional
            The mask to be applied to the face image. Pass ``None`` if no mask is to be used.
            Default ``None``.

        Returns
        -------
        :class:`TKFace`
            The created :class:`TKFace` object that has just been added to the cache.
        """
        logger.debug("Adding objects: (frame_index: %s, face: %s, landmarks: %s, mask: %s)",
                     frame_index, face.shape, landmarks.shape,
                     mask if mask is None else mask.shape)
        tk_face = TKFace(face, landmarks, mask)
        self._tk_faces[frame_index].append(tk_face)
        return tk_face

    def remove(self, frame_index, face_index):
        """ Remove a :class:`TKFace` object from the :attr:`tk_faces` for the given frame and face
        indices.

        Parameters
        ----------
        frame_index: int
            The frame index that the face is to be removed for
        face_index: int
            The face index, within the given frame, that the face is to be removed for
        """
        logger.debug("Removing objects: (frame_index: %s, face_index: %s)",
                     frame_index, face_index)
        del self._tk_faces[frame_index][face_index]

    def update(self, frame_index, face_index, face, landmarks, mask=None):
        """ Update an existing :class:`TKFace` object in the :attr:`tk_faces` for the given frame
        and face indices.

        Parameters
        ----------
        frame_index: int
            The frame index that the face is to be removed for
        face_index: int
            The face index, within the given frame, that the face is to be removed for
        face: :class:`numpy.ndarray`
            The face, sized correctly, to be updated in :attr:`tk_faces`
        landmarks: :class:`numpy.ndarray`
            The updated 68 point aligned landmarks for the given face
        mask: :class:`numpy.ndarray` or ``None``, optional
            The mask to be applied to the face image. Pass ``None`` if no mask is to be used.
            Default ``None``.
        Returns
        -------
        :class:`TKFace`
            The updated :class:`TKFace` object that has just been updated in the cache.
        """
        logger.trace("Updating objects: (frame_index: %s, face_index: %s, face: %s, "
                     "landmarks: %s, mask: %s)", frame_index, face_index, face, landmarks,
                     mask if mask is None else mask.shape)
        tk_face = self._tk_faces[frame_index][face_index]
        tk_face.update(face, landmarks, mask=mask)
        return tk_face


class TKFace():
    """ An object that holds a single :class:`tkinter.PhotoImage` face, ready for placement in the
    :class:`~tools.manual.manual.FacesViewer` canvas, along with the face's associated mesh
    annotation coordinates.

    Parameters
    ----------
    face: :class:`numpy.ndarray`
        The face, sized correctly, to create a :class:`tkinter.PhotoImage` from
    landmarks: :class:`numpy.ndarray`
        The 68 point aligned landmarks for the given face
    mask: :class:`numpy.ndarray` or ``None``, optional
        The mask to be applied to the face image. Pass ``None`` if no mask is to be used.
        Default ``None``
    """
    def __init__(self, face, landmarks, mask=None):
        logger.trace("Initializing %s: (face: %s, landmarks: %s, mask: %s)",
                     self.__class__.__name__, face.shape, landmarks.shape,
                     mask if mask is None else mask.shape)
        self._landmark_mapping = dict(mouth=(48, 68),
                                      right_eyebrow=(17, 22),
                                      left_eyebrow=(22, 27),
                                      right_eye=(36, 42),
                                      left_eye=(42, 48),
                                      nose=(27, 36),
                                      jaw=(0, 17),
                                      chin=(8, 11))
        self._face = self._generate_tk_face_data(face, mask)
        self._mesh_points, self._mesh_is_poly = self._get_mesh_points(landmarks)
        logger.trace("Initialized %s", self.__class__.__name__)

    # << PUBLIC PROPERTIES >> #
    @property
    def face(self):
        """ :class:`tkinter.PhotoImage`: The face in a format that can be placed on the
        :class:`~tools.manual.manual.FaceViewer` canvas. """
        return self._face

    @property
    def mesh_points(self):
        """ list: list of :class:`numpy.ndarray` objects containing the coordinates for each mesh
        annotation for the current face, derived from the original face's aligned landmarks. """
        return self._mesh_points

    @property
    def mesh_is_poly(self):
        """ list: `bool` values indicating whether the corresponding mesh annotation in
        :attr:`mesh_points` requires a polygon (``True``) to draw the annotation or a
        line (``False``) """
        return self._mesh_is_poly

    # << PUBLIC METHODS >> #
    def update(self, face, landmarks, mask):
        """ Update the :attr:`face`, :attr:`mesh_points` and attr:`mesh_is_poly` objects with the
        given information.

        Parameters
        ----------
        face: :class:`numpy.ndarray`
            The face, sized correctly, to be updated in :attr:`tk_faces`
        landmarks: :class:`numpy.ndarray`
            The updated 68 point aligned landmarks for the given face
        mask: :class:`numpy.ndarray` or ``None``
            The mask to be applied to the face image. Pass ``None`` if no mask is to be used
        """
        self._face = self._generate_tk_face_data(face, mask)
        self._mesh_points, self._mesh_is_poly = self._get_mesh_points(landmarks)

    def update_mask(self, mask):
        """ Update the mask in the 4th channel of :attr:`face` to the given mask.

        Parameters
        ----------
        mask: :class:`numpy.ndarray` or ``None``
            The mask to be applied to the face image. Pass ``None`` if no mask is to be used
        """
        img = cv2.imdecode(np.fromstring(self._face.cget("data"), dtype="uint8"),
                           cv2.IMREAD_UNCHANGED)
        self._face.put(self._merge_mask_to_bytes(img, mask))

    # << PRIVATE METHODS >> #
    def _generate_tk_face_data(self, face, mask):
        """ Create the :class:`tkinter.PhotoImage` face for the given face image.

        Notes
        -----
        Updating :class:`tkinter.PhotoImage` objects outside of the main loop can lead to issues.
        To protect against this, we run several attempts before failing.

        Parameters
        ----------
        face: :class:`numpy.ndarray`
            The face that is being used to create the :class:`tkinter.PhotoImage` object
        mask: :class:`numpy.ndarray` or ``None``
            The mask to add to the image. ``None`` if a mask is not being used

        Returns
        -------
        :class:`tkinter.PhotoImage`
            The face formatted for the :class:`~tools.manual.manual.FaceViewer` canvas.
        """
        face_bytes = self._merge_mask_to_bytes(face, mask)
        for attempt in range(10):
            try:
                tk_face = tk.PhotoImage(data=face_bytes)
                break
            except RuntimeError as err:
                if attempt == 9 or str(err) not in ("main thread is not in main loop",
                                                    "Too early to create image"):
                    raise
                if str(err) == "Too early to create image":
                    # GUI has gone away. Probably quit during load
                    return None
                # TODO Set to debug
                logger.info("attempt: %s: %s", attempt + 1, str(err))
                sleep(0.25)
        return tk_face

    @classmethod
    def _merge_mask_to_bytes(cls, face, mask):
        """ Generate a PNG encoded byte string with an applied mask in the 4th channel.

        Notes
        -----
        Tkinter is difficult about how it will accept data. This is currently the best method
        that I have found, but there are bound to be speed ups available if we can pass the
        image to the :class:`tkinter.PhotoImage` class without encoding to PNG first.

        Parameters
        ----------
        face: :class:`numpy.ndarray`
            The face, sized correctly, to create a :class:`tkinter.PhotoImage` from
        mask: :class:`numpy.ndarray` or ``None``, optional
            The mask to be applied to the face image. Pass ``None`` if no mask is to be used.
            Default ``None``

        Returns
        -------
        bytes
            The face image, with mask applied to 4th channel, encoded to PNG and placed in a bytes
            string
        """
        mask = np.ones_like(face.shape[:2], dtype="uint8") * 255 if mask is None else mask
        if mask.shape[0] != face.shape[0]:
            mask = cv2.resize(mask, face.shape[:2], interpolation=cv2.INTER_AREA)
        image = np.concatenate((face[..., :3], mask[..., None]), axis=-1)
        return cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 0])[1].tostring()

    def _get_mesh_points(self, landmarks):
        """ Converts aligned 68 point landmarks to mesh annotation points and flags whether a
        polygon or line is required to generate the corresponding annotation.

        Parameters
        ----------
        landmarks: :class:`numpy.ndarray`
            The 68 point aligned landmarks to be converted to mesh annotation points

        Returns
        -------
        mesh_points: list
            List of :class:`numpy.ndarray` points for each face part mesh annotation
        is_poly: list
            List of `bool` objects indicating whether the corresponding mesh point (at the same
            index) needs to be displayed as a polygon (``True``) or a line (``False``)
        """
        is_poly = []
        mesh_points = []
        for key, val in self._landmark_mapping.items():
            is_poly.append(key in ("right_eye", "left_eye", "mouth"))
            mesh_points.append(landmarks[val[0]:val[1]])
        return mesh_points, is_poly


class FaceCacheLoader():
    """ Background loader for the face cache. Loads faces into the :class:`FacesCache` in
    background threads, maintaining GUI usability.

    Parameters
    ----------
    faces_cache: :class:`FacesCache`
        The face cache that this loader will be populating faces for.
    detected_faces: :class:`~tool.manual.faces.DetectedFaces`
        The :class:`~lib.faces_detect.DetectedFace` objects for this video
    """
    def __init__(self, faces_cache, detected_faces):
        logger.debug("Initializing %s: (faces_cache: %s, detected_faces: %s)",
                     self.__class__.__name__, faces_cache, detected_faces)
        self._faces_cache = faces_cache
        self._location = faces_cache._frames.location
        self._tk_loading = faces_cache._tk_loading
        self._loaded_frame_indices = []

        self._key_frames = detected_faces.video_meta_data.get("keyframes", None)
        self._pts_times = detected_faces.video_meta_data.get("pts_time", None)
        self._saved_faces = detected_faces._saved_faces

        self._is_video = self._key_frames is not None and self._pts_times is not None
        self._num_threads = os.cpu_count() - 2
        if self._is_video:
            self._num_threads = min(self._num_threads, len(self._key_frames) - 1)
        self._executor = futures.ThreadPoolExecutor(max_workers=self._num_threads)
        logger.debug("Initialized %s", self.__class__.__name__)

    @property
    def loaded_frame_indices(self):
        """ list: Contains the frame indices that have currently been loaded. """
        return self._loaded_frame_indices

    # << PUBLIC METHODS >> #
    def set_load_complete(self):
        """ Callback to set the :attr:`FacesCache._initialized` to `True` and the
        :attr:`FacesCache._tk_loading` state to ``False``.
        Should be called after all post initial loading tasks are completed. """
        self._tk_loading.set(False)
        self._faces_cache.set_initialized()

    def launch(self):
        """ Loads the faces into the :attr:`_faces_cache.faces` scaled from 96px for GUI display.
        """
        if self._is_video:
            self._launch_video()
        else:
            self._launch_folder()

    # << PRIVATE METHODS >> #
    def _launch_video(self):
        """ Launch :class:`concurrent.futures.ThreadPoolExecutor` to retrieve faces from a
        video file.

        Splits the video into segments and passes each of these segments to separate background
        threads for some speed up.
        """
        key_frame_split = len(self._key_frames) // self._num_threads
        for idx in range(self._num_threads):
            start_idx = idx * key_frame_split
            end_idx = self._key_frames[start_idx + key_frame_split]
            start_pts = self._pts_times[self._key_frames[start_idx]]
            end_pts = False if idx + 1 == self._num_threads else self._pts_times[end_idx]
            starting_index = self._pts_times.index(start_pts)
            if end_pts:
                segment_count = len(self._pts_times[self._key_frames[start_idx]:end_idx])
            else:
                segment_count = len(self._pts_times[self._key_frames[start_idx]:])
            self._executor.submit(self._load_from_video,
                                  start_pts,
                                  end_pts,
                                  starting_index,
                                  segment_count)

    def _launch_folder(self):
        """ Launch :class:`concurrent.futures.ThreadPoolExecutor` to retrieve faces from a
        folder of images.

        Goes through the file list one at a time, passing each file to a separate background
        thread for some speed up.
        """
        reader = SingleFrameLoader(self._location)
        for idx in range(reader.count):
            self._executor.submit(self._load_from_folder, reader, idx)

    def _load_from_video(self, pts_start, pts_end, start_index, segment_count):
        """ Loads faces from video for the given segment of the source video.

        Each segment of the video is extracted from in a different background thread.

        Parameters
        ----------
        pts_start: float
            The start time to cut the segment out of the video
        pts_end: float
            The end time to cut the segment out of the video
        start_index: int
            The frame index that this segment starts from. Used for calculating the actual frame
            index of each frame extracted
        segment_count: int
            The number of frames that appear in this segment. Used for ending early in case more
            frames come out of the segment than should appear (sometimes more frames are picked up
            at the end of the segment, so these are discarded)
        """
        logger.debug("pts_start: %s, pts_end: %s, start_index: %s, segment_count: %s",
                     pts_start, pts_end, start_index, segment_count)
        reader = self._get_reader(pts_start, pts_end)
        idx = 0
        for idx, frame in enumerate(reader):
            frame_idx = idx + start_index
            self._set_face_cache_objects(frame[..., ::-1], frame_idx)
            if idx == segment_count - 1:
                # Sometimes extra frames are picked up at the end of a segment, so stop
                # processing when segment frame count has been hit.
                break
        reader.close()
        logger.debug("Segment complete: (starting_frame_index: %s, processed_count: %s)",
                     start_index, idx)

    def _get_reader(self, pts_start, pts_end):
        """ Get an imageio iterator for this thread's segment.

        Parameters
        ----------
        pts_start: float
            The start time to cut the segment out of the video
        pts_end: float
            The end time to cut the segment out of the video

        Returns
        -------
        :class:`imageio.Reader`
            A reader iterator for the requested segment of video
        """
        input_params = ["-ss", str(pts_start)]
        if pts_end:
            input_params.extend(["-to", str(pts_end)])
        logger.debug("pts_start: %s, pts_end: %s, input_params: %s",
                     pts_start, pts_end, input_params)
        return imageio.get_reader(self._location, "ffmpeg", input_params=input_params)

    def _load_from_folder(self, reader, frame_index):
        """ Loads faces from a single frame for the given frame index from a folder of images.

        Each frame is extracted in a different background thread.

        Parameters
        ----------
        reader: :class:`lib.image.SingleFrameLoader`
            The reader that is used to retrieve the requested frame
        frame_index: int
            The frame index for the image to extract faces from
        """
        _, frame = reader.image_from_index(frame_index)
        self._set_face_cache_objects(frame, frame_index)

    def _set_face_cache_objects(self, frame, frame_index):
        """ Extracts the faces from the frame, as well as the aligned landmarks.

        Populates :attr:`_faces_cache.faces` for the current frame index with the generated
        :class:`TKFace` objects.

        Appends the current frame index to :attr:`loaded_frame_indices` so that the viewer
        can pick up the latest loaded faces and display them.
        """
        tk_faces = []
        for face in self._saved_faces[frame_index]:
            face.load_aligned(frame, size=self._faces_cache.size, force=True)
            tk_faces.append(TKFace(face.aligned_face, face.aligned_landmarks, mask=None))
            face.aligned["face"] = None

        self._faces_cache.tk_faces[frame_index] = tk_faces
        self._loaded_frame_indices.append(frame_index)


class MaskLoader():
    """ Handles loading and unloading of masks to the underlying :class:`tkinter.PhotoImage`.

    Parameters
    ----------
    faces_cache: :class:`FacesCache`
        The face cache that this loader will be populating faces for.
    detected_faces: :class:`~tool.manual.faces.DetectedFaces`
        The :class:`~lib.faces_detect.DetectedFace` objects for this video
    """
    def __init__(self, faces_cache, detected_faces):
        logger.debug("Initializing %s: (faces_cache: %s, detected_faces: %s)",
                     self.__class__.__name__, faces_cache, detected_faces)
        self._faces_cache = faces_cache
        self._tk_loading = faces_cache._tk_loading
        self._canvas = faces_cache._canvas
        self._progress_bar = faces_cache._canvas._progress_bar
        self._det_faces = detected_faces
        self._current_mask_type = None
        logger.debug("Initialized %s", self.__class__.__name__)

    @property
    def _unload_masks_iterator(self):
        """ generator: Iterator for passing through all of the faces stored in
        :attr:`_faces_cache.tk_faces` for mask removal """
        return ((tk_face, None) for tk_faces in self._faces_cache.tk_faces for tk_face in tk_faces)

    @property
    def _load_masks_iterator(self):
        """ generator: Iterator for passing through all of the faces stored in
        :attr:`_faces_cache.tk_faces` for mask application """
        latest_faces = self._det_faces.current_faces
        return ((tk_face, face)
                for faces, tk_faces in zip(latest_faces, self._faces_cache.tk_faces)
                for face, tk_face in zip(faces, tk_faces))

    # << PUBLIC METHODS >> #
    def update_all(self, mask_type, is_enabled):
        """ Update the mask for every face that exists in :attr:`_faces_cache.faces`.

        Parameters
        ----------
        mask_type: str
            The type of mask to display on the face
        is_enabled: bool
            ``True`` if the optional annotation button is enabled for mask view otherwise ``False``
        """
        mask_type = None if not is_enabled or mask_type == "" else mask_type.lower()
        if self._faces_cache.is_loading or mask_type == self._current_mask_type:
            return
        self._tk_loading.set(True)
        self._progress_bar.start(mode="determinate")
        executor = self._load_unload_masks(mask_type)
        total_faces = sum(1 for tk_faces in self._faces_cache.tk_faces for tk_face in tk_faces)
        self._update_display(executor, mask_type, total_faces)
        self._current_mask_type = mask_type

    def update_selected(self, frame_index, mask_type):
        """ Update the mask image for the faces in the given frame index.

        Parameters
        ----------
        frame_index: int
            The frame index to update the faces for
        mask_type: str
            The type of mask to display on the face
        """
        logger.trace("Updating selected faces: (frame_index: %s, mask_type: %s)",
                     frame_index, mask_type)
        mask_type = mask_type if mask_type is None else mask_type.lower()
        faces = self._det_faces.current_faces[frame_index]
        for face, tk_face in zip(faces, self._faces_cache.tk_faces[frame_index]):
            tk_face.update_mask(self._get_mask(mask_type, face))

    # << PRIVATE METHODS >> #
    def _update_display(self, face_futures, mask_type, total_faces, processed_count=0):
        """ Update the :class:`~manual.manual.FacesViewer` canvas with the latest loaded masked
        faces.

        Updates every 0.5 seconds with the currently available images until all images have been
        loaded when the cycle completes.

        Parameters
        ----------
        face_futures: list
            List of :class:`concurrent.futures.future` objects that are being executed to load
            the masked faces.
        mask_type: str or ``None``
            The type of mask to display on the face or ``None`` if the mask is being removed
        total_faces: int
            The total number of faces that are to be processed
        processed_count: int
            The number of faces that have been processed so fate
        """
        to_process = [face_futures.pop(face_futures.index(future)) for future in list(face_futures)
                      if future.done()]
        processed_count += len(to_process)
        self._update_progress(mask_type, processed_count, total_faces)
        if not face_futures:
            self._progress_bar.stop()
            self._tk_loading.set(False)
            return
        self._canvas.after(500,
                           self._update_display,
                           face_futures,
                           mask_type,
                           total_faces,
                           processed_count)

    def _load_unload_masks(self, mask_type):
        """ Load or unload masks from the 4th channel of the :class:`tkinter.PhotoImage`.

        The update is performed in background threads for some speed up.

        Parameters
        ----------
        mask_type: str
            The type of mask to display on the face

        Returns
        -------
        list
            List of :class:`concurrent.futures.future` objects for the executed background threads
        """
        iterator = self._unload_masks_iterator if mask_type is None else self._load_masks_iterator
        executor = futures.ThreadPoolExecutor(max_workers=os.cpu_count() - 1)
        futures_update = [executor.submit(self._update_bytes_mask,
                                          tk_face,
                                          mask_type,
                                          face)
                          for tk_face, face in iterator]
        return futures_update

    def _get_mask(self, mask_type, detected_face):
        """ Obtain the mask from the alignments file.

        Parameters
        ----------
        mask_type: str
            The mask type to retrieve from the alignments file. ``None`` if any mask should be
            removed
        detected_face: :class:`~lib.faces_detect.DetectedFace`
            The detected face object to extract the mask from

        Returns
        -------
        :class:`numpy.ndarray` or ``None``
            If the mask type is not ``None`` and exists within the alignments file, then it is
            returned as a single channel image, otherwise ``None`` is returned
        """
        if mask_type is None:
            return None
        mask_class = detected_face.mask.get(mask_type, None)
        if mask_class is None:
            return None
        mask = mask_class.mask.squeeze()
        if mask.shape[0] != self._faces_cache.size:
            mask = cv2.resize(mask,
                              (self._faces_cache.size, self._faces_cache.size),
                              interpolation=cv2.INTER_AREA)
        return mask

    def _update_bytes_mask(self, tk_face, mask_type, detected_face=None):
        """ Update the :class:`tkinter.PhotoImage` to include the given mask.

        Parameters
        ----------
        tk_face: :class:`TKFace`
            The tk face object containing the :class:`tkinter.PhotoImage` that the mask is to be
            updated to
        mask_type: str or `None`
            The mask type to update onto the :class:`tkinter.PhotoImage`. Pass `None` if mask is to
            be removed
        detected_face: class:`~lib.faces_detect.DetectedFace` or `None`
            The detected face object to extract the mask from. Can pass `None` if a mask is just
            being removed
        """
        mask = None if detected_face is None else self._get_mask(mask_type, detected_face)
        tk_face.update_mask(mask)

    def _update_progress(self, mask_type, position, total_count):
        """ Update the progress bar with the current mask update progress.

        mask_type: str or ``None``
            The mask type that is being updated. ``None`` if any mask is being removed
        position: int
            The number of masks which have been updated
        total_count: int
            The total number of masks that are to be updated
        """
        progress = int(round((position / total_count) * 100))
        msg = "Removing Mask: " if mask_type is None else "Loading {} Mask: ".format(mask_type)
        msg += "{}/{} - {}%".format(position, total_count, progress)
        self._progress_bar.progress_update(msg, progress)