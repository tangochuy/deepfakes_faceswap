#!/usr/bin python3
""" Utilities available across all scripts """

import os
from os.path import basename, exists
import queue as Queue
import threading
import warnings

from pathlib import Path

import cv2

def get_folder(path):
    """ Return a path to a folder, creating it if it doesn't exist """
    output_dir = Path(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir

def get_image_paths(directory, exclude=list(), debug=False):
    """ Return a list of images that reside in a folder """
    image_extensions = [".jpg", ".jpeg", ".png", ".tif", ".tiff"]
    exclude_names = [basename(Path(x).stem[:-2] + Path(x).suffix) for x in exclude]
    dir_contents = list()

    if not exists(directory):
        directory = get_folder(directory)

    dir_scanned = sorted(os.scandir(directory), key=lambda x: x.name)
    for chkfile in dir_scanned:
        if any([chkfile.name.lower().endswith(ext) for ext in image_extensions]):
            if chkfile.name in exclude_names:
                if debug:
                    print("Already processed %s" % chkfile.name)
                continue
            else:
                dir_contents.append(chkfile.path)

    return dir_contents

def rotate_image(image, angle, rotated_width=None, rotated_height=None):
    """ Rotate an image by a given angle. From:
        https://stackoverflow.com/questions/22041699 """
    height, width = image.shape[:2]
    image_center = (width/2, height/2)
    rotation_matrix = cv2.getRotationMatrix2D(image_center, -1.*angle, 1.)
    if rotated_width is None or rotated_height is None:
        abs_cos = abs(rotation_matrix[0, 0])
        abs_sin = abs(rotation_matrix[0, 1])
        if rotated_width is None:
            rotated_width = int(height*abs_sin + width*abs_cos)
        if rotated_height is None:
            rotated_height = int(height*abs_cos + width*abs_sin)
    rotation_matrix[0, 2] += rotated_width/2 - image_center[0]
    rotation_matrix[1, 2] += rotated_height/2 - image_center[1]
    return cv2.warpAffine(image, rotation_matrix, (rotated_width, rotated_height))

def set_system_verbosity(loglevel):
    """ Set the verbosity level of tensorflow and suppresses
        future and deprecation warnings from any modules
        From:
        https://stackoverflow.com/questions/35911252/disable-tensorflow-debugging-information
        Can be set to:
        0 - all logs shown
        1 - filter out INFO logs
        2 - filter out WARNING logs
        3 - filter out ERROR logs  """
    # TODO suppress tensorflow deprecation warnings """

    os.environ['TF_CPP_MIN_LOG_LEVEL'] = loglevel
    if loglevel != '0':
        for warncat in (FutureWarning, DeprecationWarning):
            warnings.simplefilter(action='ignore', category=warncat)

class BackgroundGenerator(threading.Thread):
    """ Run a queue in the background. From:
        https://stackoverflow.com/questions/7323664/python-generator-pre-fetch """
    def __init__(self, generator, prefetch=1): #See below why prefetch count is flawed
        threading.Thread.__init__(self)
        self.queue = Queue.Queue(prefetch)
        self.generator = generator
        self.daemon = True
        self.start()

    def run(self):
        """ Put until queue size is reached.
            Note: put blocks only if put is called while queue has already reached max size
            => this makes 2 prefetched items! One in the queue, one waiting for insertion! """
        for item in self.generator:
            self.queue.put(item)
        self.queue.put(None)

    def iterator(self):
        """ Iterate items out of the queue """
        while True:
            next_item = self.queue.get()
            if next_item is None:
                break
            yield next_item
