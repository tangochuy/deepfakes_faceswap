#!/usr/bin/env python3
""" Base class for Models. ALL Models should at least inherit from this class

    When inheriting model_data should be a list of NNMeta objects.
    See the class for details.
"""
import inspect
import logging
import os
import platform
import sys
import time

from concurrent import futures
from contextlib import nullcontext

import tensorflow as tf

from keras import losses
from keras import backend as K
from keras.layers import Input, Layer
from keras.models import load_model
from keras.optimizers import Adam
from keras.utils import get_custom_objects

from lib.serializer import get_serializer
from lib.model.backup_restore import Backup
from lib.model.losses import (DSSIMObjective, PenalizedLoss, gradient_loss, mask_loss_wrapper,
                              generalized_loss, l_inf_norm, gmsd_loss, gaussian_blur)
from lib.model.nn_blocks import set_config as set_nnblock_config
from lib.utils import deprecation_warning, get_backend, FaceswapError
from plugins.train._config import Config

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
_CONFIG = None

# TODO Legacy is removed. Still check for legacy and give instructions for updating by using TF1.15
# TODO Mask Input


class ModelBase():
    """ Base class that all models should inherit from.

    Parameters
    ----------
    model_dir: str
        The full path to the model save location
    arguments: :class:`argparse.Namespace`
        The arguments that were passed to the train or convert process as generated from
        Faceswap's command line arguments
    """
    def __init__(self,
                 model_dir,
                 arguments,
                 snapshot_interval=0,
                 warp_to_landmarks=False,
                 augment_color=True,
                 no_flip=False,
                 training_image_size=256,
                 alignments_paths=None,
                 preview_scale=100,
                 predict=False):
        logger.debug("Initializing ModelBase (%s): (model_dir: '%s', arguments: %s, "
                     "snapshot_interval: %s, warp_to_landmarks: %s, "
                     "augment_color: %s, no_flip: %s, training_image_size, %s, alignments_paths: "
                     "%s, preview_scale: %s, predict: %s)",
                     self.__class__.__name__, model_dir, arguments, snapshot_interval,
                     warp_to_landmarks, augment_color, no_flip, training_image_size,
                     alignments_paths, preview_scale, predict)

        self.input_shape = None  # Must be set within the plugin after initializing
        self.output_shape = None  # Must be set within the plugin after initializing
        self.trainer = "original"  # Override for plugin specific trainer

        self._model_dir = model_dir
        self._args = arguments
        self._is_predict = predict
        self._configfile = arguments.configfile if hasattr(arguments, "configfile") else None

        self._model = None
        self._backup = Backup(self._model_dir, self.name)

        self._load_config()  # Load config if plugin has not already referenced it
        self._strategy = Strategy(self._args.distribution)

        self.state = State(self._model_dir,
                           self.name,
                           self._config_changeable_items,
                           self._args.no_logs,
                           training_image_size)

        self.load_state_info()

        # The variables holding masks if Penalized Loss is used
        self._mask_variables = dict(a=None, b=None)
        self.predictors = dict()  # Predictors for model
        self.history = dict()  # Loss history per save iteration)

        # Training information specific to the model should be placed in this
        # dict for reference by the trainer.
        self.training_opts = {"alignments": alignments_paths,
                              "preview_scaling": preview_scale / 100,
                              "warp_to_landmarks": warp_to_landmarks,
                              "augment_color": augment_color,
                              "no_flip": no_flip,
                              "snapshot_interval": snapshot_interval,
                              "training_size": self.state.training_size,
                              "no_logs": self.state.current_session["no_logs"],
                              "coverage_ratio": self.calculate_coverage_ratio(),
                              "mask_type": self.config["mask_type"],
                              "mask_blur_kernel": self.config["mask_blur_kernel"],
                              "mask_threshold": self.config["mask_threshold"],
                              "learn_mask": (self.config["learn_mask"] and
                                             self.config["mask_type"] is not None),
                              "penalized_mask_loss": self.config["penalized_mask_loss"]}
        logger.debug("training_opts: %s", self.training_opts)

        if self._multiple_models_in_folder:
            deprecation_warning("Support for multiple model types within the same folder",
                                additional_info="Please split each model into separate folders to "
                                                "avoid issues in future.")
        logger.debug("Initialized ModelBase (%s)", self.__class__.__name__)

    @property
    def model_dir(self):
        """str: The full path to the model folder location. """
        return self._model_dir

    @property
    def _filename(self):
        """str: The filename for this model."""
        return os.path.join(self._model_dir, "{}.h5".format(self.name))

    @property
    def _model_exists(self):
        """ bool: ``True`` if a model of the type being loaded exists within the model folder
        location otherwise ``False``
        """
        return os.path.isfile(self._filename)

    @property
    def _config_section(self):
        """ str: The section name for the current plugin for loading configuration options from the
        config file. """
        return ".".join(self.__module__.split(".")[-2:])

    @property
    def _config_changeable_items(self):
        """ dict: The configuration options that can be updated after the model has already been
            created. """
        return Config(self._config_section, configfile=self._configfile).changeable_items

    # TODO
    @property
    def _multiple_models_in_folder(self):
        """ :bool: ``True`` if there are multiple model types in the same folder otherwise
        ``false``. """
        model_files = [fname for fname in os.listdir(self._model_dir) if fname.endswith(".h5")]
        retval = False if not model_files else os.path.commonprefix(model_files) == ""
        logger.debug("model_files: %s, retval: %s", model_files, retval)
        return retval

    @property
    def config(self):
        """ dict: The configuration dictionary for current plugin. """
        # TODO Check if config still needs to be globalled now that build is called explicitly
        global _CONFIG  # pylint: disable=global-statement
        if not _CONFIG:
            model_name = self._config_section
            logger.debug("Loading config for: %s", model_name)
            _CONFIG = Config(model_name, configfile=self._configfile).config_dict
        return _CONFIG

    @property
    def name(self):
        """ str: The name of this model based on the plugin name. """
        basename = os.path.basename(sys.modules[self.__module__].__file__)
        return os.path.splitext(basename)[0].lower()

    @property
    def output_shapes(self):
        """ list: A list of shape tuples for the output of the model """
        # TODO Currently we're pulling all of the outputs and I'm just extracting from the first
        # side. This is not right, Need to fix this to properly output, especially when masks are
        # involved
        retval = [tuple(K.int_shape(output)[-3:]) for output in self._model.outputs]
        retval = [retval[0]]
        return retval

    # TODO
#    @property
#    def output_shape(self):
#        """ The output shape of the model (shape of largest face output) """
#        return self.output_shapes[self.largest_face_index]

    @property
    def largest_face_index(self):
        """ Return the index from model.outputs of the largest face
            Required for multi-output model prediction. The largest face
            is assumed to be the final output
        """
        sizes = [shape[1] for shape in self.output_shapes if shape[2] == 3]
        if not sizes:
            return None
        max_face = max(sizes)
        retval = [idx for idx, shape in enumerate(self.output_shapes)
                  if shape[1] == max_face and shape[2] == 3][0]
        logger.debug(retval)
        return retval

    @property
    def largest_mask_index(self):
        """ Return the index from model.outputs of the largest mask
            Required for multi-output model prediction. The largest face
            is assumed to be the final output
        """
        sizes = [shape[1] for shape in self.output_shapes if shape[2] == 1]
        if not sizes:
            return None
        max_mask = max(sizes)
        retval = [idx for idx, shape in enumerate(self.output_shapes)
                  if shape[1] == max_mask and shape[2] == 1][0]
        logger.debug(retval)
        return retval

    @property
    def feed_mask(self):
        """ bool: ``True`` if the model expects a mask to be fed into input otherwise ``False`` """
        return self.config["mask_type"] is not None and self.config["learn_mask"]

    @property
    def mask_variables(self):
        """ dict: for each side a :class:`keras.backend.variable` or ``None``.
        If Penalized Mask Loss is used then each side will return a Variable of
        (`batch size`, `h`, `w`, 1) corresponding to the size of the model input.
        If Penalized Mask Loss is not used then each side will return ``None``

        Raises
        ------
        FaceswapError:
            If Penalized Mask Loss has been selected, but a mask type has not been specified
        """
        if not self.config["penalized_mask_loss"] or all(val is not None
                                                         for val in self._mask_variables.values()):
            return self._mask_variables

        if self.config["penalized_mask_loss"] and self.config["mask_type"] is None:
            raise FaceswapError("Penalized Mask Loss has been selected but you have not chosen a "
                                "Mask to use. Please select a mask or disable Penalized Mask "
                                "Loss.")

        output_network = [network for network in self.networks.values() if network.is_output][0]
        mask_shape = output_network.output_shapes[-1][:-1] + (1, )
        for side in ("a", "b"):
            var = K.variable(K.ones((self._args.batch_size, ) + mask_shape[1:], dtype="float32"),
                             dtype="float32",
                             name="penalized_mask_variable_{}".format(side))
            if get_backend() != "amd":
                # trainable and shape don't have a setter, so we need to go to private property
                var._trainable = False  # pylint:disable=protected-access
                var._shape = tf.TensorShape(mask_shape)  # pylint:disable=protected-access
            self._mask_variables[side] = var
        logger.debug("Created mask variables: %s", self._mask_variables)
        return self._mask_variables

    def _load_config(self):
        """ Load the global config for reference in :attr:`config` and set the faceswap blocks
        configuration options in `lib.model.nn_blocks` """
        global _CONFIG  # pylint: disable=global-statement
        if not _CONFIG:
            model_name = self._config_section
            logger.debug("Loading config for: %s", model_name)
            _CONFIG = Config(model_name, configfile=self._configfile).config_dict

        nn_block_keys = ['icnr_init', 'conv_aware_init', 'reflect_padding']
        set_nnblock_config({key: _CONFIG.pop(key)
                            for key in nn_block_keys})

    def calculate_coverage_ratio(self):
        """ Coverage must be a ratio, leading to a cropped shape divisible by 2 """
        coverage_ratio = self.config.get("coverage", 62.5) / 100
        logger.debug("Requested coverage_ratio: %s", coverage_ratio)
        cropped_size = (self.state.training_size * coverage_ratio) // 2 * 2
        coverage_ratio = cropped_size / self.state.training_size
        logger.debug("Final coverage_ratio: %s", coverage_ratio)
        return coverage_ratio

    def build(self):
        """ Build the model. Override for custom build methods """
        with self._strategy.scope():
            if self._model_exists:
                self._load_model()
            else:
                inputs = self.get_inputs()
                self._model = self.build_model(inputs)
            self._compile_model(initialize=True)
        if not self._is_predict:
            self._model.summary(print_fn=lambda x: logger.verbose("%s", x))

    def get_inputs(self):
        """ Return the inputs for the model """
        logger.debug("Getting inputs")
        if self.feed_mask:
            mask_shape = self.output_shape[:2] + (1, )
            logger.info("mask_shape: %s", mask_shape)
        inputs = []
        for side in ("a", "b"):
            face_in = Input(shape=self.input_shape, name="face_in_{}".format(side))
            if self.feed_mask:
                mask_in = Input(shape=mask_shape, name="mask_in_{}".format(side))
                inputs.append([face_in, mask_in])
            else:
                inputs.append(face_in)
        logger.debug("inputs: %s", inputs)
        return inputs

    def build_model(self, inputs):
        """ Override for Model Specific autoencoder builds

            Inputs is defined in self.get_inputs() and is standardized for all models
                if will generally be in the order:
                [face (the input for image),
                 mask (the input for mask if it is used)]
        """
        raise NotImplementedError

    def load_state_info(self):
        """ Load the input shape from state file if it exists """
        logger.debug("Loading Input Shape from State file")
        if not self.state.inputs:
            logger.debug("No input shapes saved. Using model config")
            return
        if not self.state.face_shapes:
            logger.warning("Input shapes stored in State file, but no matches for 'face'."
                           "Using model config")
            return
        input_shape = self.state.face_shapes[0]
        logger.debug("Setting input shape from state file: %s", input_shape)
        self.input_shape = input_shape

    # TODO (store inputs)
    def add_predictor(self, side, model):
        """ Add a predictor to the predictors dictionary """
        logger.debug("Adding predictor: (side: '%s', model: %s)", side, model)
        self.predictors[side] = model
        if not self.state.inputs:
            self.store_input_shapes(model)

    def store_input_shapes(self, model):
        """ Store the input and output shapes to state """
        logger.debug("Adding input shapes to state for model")
        inputs = {tensor.name: K.int_shape(tensor)[-3:] for tensor in model.inputs}
        if not any(inp for inp in inputs.keys() if inp.startswith("face")):
            raise ValueError("No input named 'face' was found. Check your input naming. "
                             "Current input names: {}".format(inputs))
        # Make sure they are all ints so that it can be json serialized
        inputs = {key: tuple(int(i) for i in val) for key, val in inputs.items()}
        self.state.inputs = inputs
        logger.debug("Added input shapes: %s", self.state.inputs)

    def _compile_model(self, initialize=True):
        """ Compile the model to include the Optimizer and Loss Function.

        If the model is being compiled for the first time then add the loss names to the
        state file.

        Parameters
        ----------
        initialize: bool
            ``True`` if this is a new model otherwise ``False``
        """
        # TODO Remove initialize parameter and decide pathway based on if model file exists
        logger.debug("Compiling Predictors")
        optimizer = self._get_optimizer()
        loss = Loss(self._model.inputs, self._model.outputs, None)
        self._model.compile(optimizer=optimizer, loss=loss.funcs)
        if initialize:
            self.state.add_session_loss_names("both", loss.names)
        logger.info("Compiled Predictors. Losses: %s", loss.names)

    def _get_optimizer(self):
        """ Return a Keras Adam Optimizer with user selected parameters.

        Returns
        -------
        :class:`keras.optimizers.Adam`
            An Adam Optimizer with the given user settings

        Notes
        -----
        Clip-norm is ballooning VRAM usage, which is not expected behavior and may be a bug in
        Keras/Tensorflow.

        PlaidML has a bug regarding the clip-norm parameter See:
        https://github.com/plaidml/plaidml/issues/228. We workaround by simply not adding this
        parameter for AMD backend users.
        """
        # TODO add clipnorm in for plaidML when it is fixed in the main repository
        clipnorm = get_backend() != "amd" and self.config.get("clipnorm", False)
        if self._strategy.use_strategy and clipnorm:
            logger.warning("Clipnorm has been selected, but is unsupported when using "
                           "distribution strategies, so has been disabled. If you wish to enable "
                           "clipnorm, then you must use the `default` distribution strategy.")
        if self._strategy.use_strategy:
            # Tensorflow checks whether clipnorm is None rather than False when using distribution
            # strategy so we need to explicitly set it to None.
            clipnorm = None
        learning_rate = "lr" if get_backend() == "amd" else "learning_rate"
        kwargs = dict(beta_1=0.5,
                      beta_2=0.99,
                      clipnorm=clipnorm)
        kwargs[learning_rate] = self.config.get("learning_rate", 5e-5)
        retval = Adam(**kwargs)
        logger.debug("Optimizer: %s, kwargs: %s", retval, kwargs)
        return retval

    # TODO
    def converter(self, swap):
        """ Converter for autoencoder models """
        logger.debug("Getting Converter: (swap: %s)", swap)
        side = "a" if swap else "b"
        model = self.predictors[side]
        if self._is_predict:
            # Must compile the model to be thread safe
            model._make_predict_function()  # pylint: disable=protected-access
        retval = model.predict
        logger.debug("Got Converter: %s", retval)
        return retval

    @property
    def iterations(self):
        "Get current training iteration number"
        return self.state.iterations

    def map_models(self, swapped):
        """ Map the models for A/B side for swapping """
        logger.debug("Map models: (swapped: %s)", swapped)
        models_map = {"a": dict(), "b": dict()}
        sides = ("a", "b") if not swapped else ("b", "a")
        for network in self.networks.values():
            if network.side == sides[0]:
                models_map["a"][network.type] = network.filename
            if network.side == sides[1]:
                models_map["b"][network.type] = network.filename
        logger.debug("Mapped models: (models_map: %s)", models_map)
        return models_map

    def do_snapshot(self):
        """ Perform a model snapshot """
        logger.debug("Performing snapshot")
        self._backup.snapshot_models(self.iterations)
        logger.debug("Performed snapshot")

    def _load_model(self):
        """ Loads the model from disk and sets to :attr:`_model`

        If the predict function is to be called and the model cannot be found in the model folder
        then an error is logged and the process exits.

        When loading the model, the plugin model folder is scanned for custom layers which are
        added to Keras' custom objects.
        """
        logger.debug("Loading model: %s", self._filename)
        if self._is_predict and not self._model_exists:
            logger.error("Model could not be found in folder '%s'. Exiting", self._model_dir)
            sys.exit(1)

        if not self._is_predict:
            K.clear_session()
        self._add_custom_objects()
        self._model = load_model(self._filename)
        logger.info("Loaded model from disk: '%s'", self._filename)

    def _add_custom_objects(self):
        """ Add the plugin's layers to Keras custom objects """
        custom_objects = {name: obj
                          for name, obj in inspect.getmembers(sys.modules[self.__module__])
                          if (inspect.isclass(obj)
                              and obj.__module__ == self.__module__
                              and Layer in obj.__bases__)}
        logger.debug("Adding custom objects: %s", custom_objects)
        get_custom_objects().update(custom_objects)

    def save_models(self):
        """ Backup and save the models """
        # TODO
        self._model.save(self._filename, include_optimizer=False)
        return
        logger.debug("Backing up and saving models")
        # Insert a new line to avoid spamming the same row as loss output
        print("")
        save_averages = self.get_save_averages()
        backup_func = self._backup.backup_model if self.should_backup(save_averages) else None
        if backup_func:
            logger.info("Backing up models...")
        executor = futures.ThreadPoolExecutor()
        save_threads = [executor.submit(network.save, backup_func=backup_func)
                        for network in self.networks.values()]
        save_threads.append(executor.submit(self.state.save, backup_func=backup_func))
        futures.wait(save_threads)
        # call result() to capture errors
        _ = [thread.result() for thread in save_threads]
        msg = "[Saved models]"
        if save_averages:
            lossmsg = ["{}_{}: {:.5f}".format(self.state.loss_names[side][0],
                                              side.capitalize(),
                                              save_averages[side])
                       for side in sorted(list(save_averages.keys()))]
            msg += " - Average since last save: {}".format(", ".join(lossmsg))
        logger.info(msg)

    def get_save_averages(self):
        """ Return the average loss since the last save iteration and reset historical loss """
        logger.debug("Getting save averages")
        avgs = dict()
        for side, loss in self.history.items():
            if not loss:
                logger.debug("No loss in self.history: %s", side)
                break
            avgs[side] = sum(loss) / len(loss)
            self.history[side] = list()  # Reset historical loss
        logger.debug("Average losses since last save: %s", avgs)
        return avgs

    def should_backup(self, save_averages):
        """ Check whether the loss averages for all losses is the lowest that has been seen.

            This protects against model corruption by only backing up the model
            if any of the loss values have fallen.
            TODO This is not a perfect system. If the model corrupts on save_iteration - 1
            then model may still backup
        """
        backup = True

        if not save_averages:
            logger.debug("No save averages. Not backing up")
            return False

        for side, loss in save_averages.items():
            if not self.state.lowest_avg_loss.get(side, None):
                logger.debug("Setting initial save iteration loss average for '%s': %s",
                             side, loss)
                self.state.lowest_avg_loss[side] = loss
                continue
            if backup:
                # Only run this if backup is true. All losses must have dropped for a valid backup
                backup = self.check_loss_drop(side, loss)

        logger.debug("Lowest historical save iteration loss average: %s",
                     self.state.lowest_avg_loss)

        if backup:  # Update lowest loss values to the state
            for side, avg_loss in save_averages.items():
                logger.debug("Updating lowest save iteration average for '%s': %s", side, avg_loss)
                self.state.lowest_avg_loss[side] = avg_loss

        logger.debug("Backing up: %s", backup)
        return backup

    def check_loss_drop(self, side, avg):
        """ Check whether total loss has dropped since lowest loss """
        if avg < self.state.lowest_avg_loss[side]:
            logger.debug("Loss for '%s' has dropped", side)
            return True
        logger.debug("Loss for '%s' has not dropped", side)
        return False


class Strategy():
    """ Distribution Strategies for Tensorflow.

    Tensorflow 2 uses distribution strategies for multi-GPU/system training. These are context
    managers. To enable the code to be more readable, we handle strategies the same way for Nvidia
    and AMD backends. PlaidML does not support strategies, but we need to still create a context
    manager so that we don't need branching logic.

    Parameters
    ----------
    strategy: ["default", "mirror", "central"]
        The required strategy. `"default"` effectively means 'do not explicitly provide a strategy'
        and let's Tensorflow handle things for itself. `"mirror" is Tensorflow Mirrored Strategy.
        "`central`" is Tensorflow Central Storage Strategy with variables explicitly placed on the
        CPU.
    """
    def __init__(self, strategy):
        logger.debug("Initializing %s: (strategy: %s)", self.__class__.__name__, strategy)
        self._strategy = self._get_strategy(strategy)
        logger.debug("Initialized %s", self.__class__.__name__)

    @property
    def use_strategy(self):
        """ bool: ``True`` if a distribution strategy is to be used otherwise ``False``. """
        return self._strategy is not None

    @staticmethod
    def _get_strategy(strategy):
        """ If we are running on Nvidia backend and the strategy is not `"default"` then return
        the correct tensorflow distribution strategy, otherwise return ``None``.

        Notes
        -----
        By default Tensorflow defaults mirrored strategy to use the Nvidia NCCL method for
        reductions, however this is only available in Linux, so the method used falls back to
        `Hierarchical Copy All Reduce` if the OS is not Linux.

        Parameters
        ----------
        strategy: str
            The request training strategy to use

        Returns
        -------
        :class:`tensorflow.python.distribute.Strategy` or `None`
            The request Tensorflow Strategy if the backend is Nvidia and the strategy is not
            `"Default"` otherwise ``None``
        """
        if get_backend() != "nvidia":
            retval = None
        elif strategy == "mirror":
            if platform.system().lower() == "linux":
                cross_device_ops = tf.distribute.NcclAllReduce()
            else:
                cross_device_ops = tf.distribute.HierarchicalCopyAllReduce()
            logger.debug("cross_device_ops: %s", cross_device_ops)
            retval = tf.distribute.MirroredStrategy(cross_device_ops=cross_device_ops)
        elif strategy == "central":
            retval = tf.distribute.experimental.CentralStorageStrategy(parameter_device="/CPU:0")
        else:
            retval = tf.distribute.get_strategy()
        logger.debug("Using strategy: %s", retval)
        return retval

    def scope(self):
        """ Return the strategy scope if we have set a strategy, otherwise return a null
        context.

        Returns
        -------
        :func:`tensorflow.python.distribute.Strategy.scope` or :func:`contextlib.nullcontext`
            The tensorflow strategy scope if a strategy is valid in the current scenario. A null
            context manager if the strategy is not valid in the current scenario
        """
        retval = nullcontext() if self._strategy is None else self._strategy.scope()
        logger.debug("Using strategy scope: %s", retval)
        return retval


class Loss():
    """ Holds loss names and functions for an Autoencoder """
    def __init__(self, inputs, outputs, mask_variable):
        logger.debug("Initializing %s: (inputs: %s, outputs: %s, mask_variable: %s)",
                     self.__class__.__name__, inputs, outputs, mask_variable)
        self.inputs = inputs
        self.outputs = outputs
        self._mask_variable = mask_variable
        self.names = self.get_loss_names()
        self.funcs = self.get_loss_functions()
        if len(self.names) > 1:
            self.names.insert(0, "total_loss")
        logger.debug("Initialized: %s", self.__class__.__name__)

    @property
    def loss_dict(self):
        """ Return the loss dict """
        loss_dict = dict(mae=losses.mean_absolute_error,
                         mse=losses.mean_squared_error,
                         logcosh=losses.logcosh,
                         smooth_loss=generalized_loss,
                         l_inf_norm=l_inf_norm,
                         ssim=DSSIMObjective(),
                         gmsd=gmsd_loss,
                         pixel_gradient_diff=gradient_loss)
        return loss_dict

    @property
    def config(self):
        """ Return the global _CONFIG variable """
        return _CONFIG

    @property
    def mask_preprocessing_func(self):
        """ The selected pre-processing function for the mask """
        retval = None
        if self.config.get("mask_blur", False):
            retval = gaussian_blur(max(1, self.mask_shape[1] // 32))
        logger.debug(retval)
        return retval

    @property
    def selected_loss(self):
        """ Return the selected loss function """
        retval = self.loss_dict[self.config.get("loss_function", "mae")]
        logger.debug(retval)
        return retval

    @property
    def selected_mask_loss(self):
        """ Return the selected mask loss function. Currently returns mse
            If a processing function has been requested wrap the loss function
            in loss wrapper """
        loss_func = self.loss_dict["mse"]
        func = self.mask_preprocessing_func
        logger.debug("loss_func: %s, func: %s", loss_func, func)
        retval = mask_loss_wrapper(loss_func, preprocessing_func=func)
        return retval

    @property
    def output_shapes(self):
        """ The shapes of the output nodes """
        return [K.int_shape(output)[1:] for output in self.outputs]

    @property
    def mask_input(self):
        """ Return the mask input or None """
        mask_inputs = [inp for inp in self.inputs if inp.name.startswith("mask")]
        if not mask_inputs:
            return None
        return mask_inputs[0]

    @property
    def mask_shape(self):
        """ Return the mask shape """
        if self.mask_input is None and self._mask_variable is None:
            return None
        if self.mask_input:
            retval = K.int_shape(self.mask_input)[1:]
        else:
            retval = K.int_shape(self._mask_variable)
        return retval

    def get_loss_names(self):
        """ Return the loss names based on model output """
        output_names = [output.name for output in self.outputs]
        logger.info("Model output names: %s", output_names)
        loss_names = [name[name.find("/") + 1:name.rfind("/")].replace("_out", "")
                      for name in output_names]
        if not all(name.startswith("face") or name.startswith("mask") for name in loss_names):
            # Handle incorrectly named/legacy outputs
            logger.info("Renaming loss names from: %s", loss_names)
            loss_names = self.update_loss_names()
        loss_names = ["{}_loss".format(name) for name in loss_names]
        logger.info(loss_names)
        return loss_names

    def update_loss_names(self):
        """ Update loss names if named incorrectly or legacy model """
        output_types = ["mask" if shape[-1] == 1 else "face" for shape in self.output_shapes]
        loss_names = ["{}{}".format(name,
                                    "" if output_types.count(name) == 1 else "_{}".format(idx))
                      for idx, name in enumerate(output_types)]
        logger.debug("Renamed loss names to: %s", loss_names)
        return loss_names

    def get_loss_functions(self):
        """ Set the loss function """
        loss_funcs = []
        for idx, loss_name in enumerate(self.names):
            if loss_name.startswith("mask"):
                loss_funcs.append(self.selected_mask_loss)
            elif self.config["penalized_mask_loss"] and self.config["mask_type"] is not None:
                face_size = self.output_shapes[idx][1]
                mask_size = self.mask_shape[1]
                scaling = face_size / mask_size
                logger.debug("face_size: %s mask_size: %s, mask_scaling: %s",
                             face_size, mask_size, scaling)
                loss_funcs.append(PenalizedLoss(self._mask_variable, self.selected_loss,
                                                mask_scaling=scaling,
                                                preprocessing_func=self.mask_preprocessing_func))
            else:
                loss_funcs.append(self.selected_loss)
            logger.debug("%s: %s", loss_name, loss_funcs[-1])
        logger.debug(loss_funcs)
        return loss_funcs


class NNMeta():
    """ Class to hold a neural network and it's meta data

    filename:   The full path and filename of the model file for this network.
    type:       The type of network. For networks that can be swapped
                The type should be identical for the corresponding
                A and B networks, and should be unique for every A/B pair.
                Otherwise the type should be completely unique.
    side:       A, B or None. Used to identify which networks can
                be swapped.
    network:    Define network to this.
    is_output:  Set to True to indicate that this network is an output to the Autoencoder
    """

    def __init__(self, filename, network_type, side, network, is_output):
        logger.debug("Initializing %s: (filename: '%s', network_type: '%s', side: '%s', "
                     "network: %s, is_output: %s", self.__class__.__name__, filename,
                     network_type, side, network, is_output)
        self.filename = filename
        self.type = network_type.lower()
        self.side = side
        self.name = self.set_name()
        self.network = network
        self.is_output = is_output
        if get_backend() == "amd":
            self.network.name = self.name
        else:
            # No setter in tensorflow.keras
            self.network._name = self.name
        self.config = network.get_config()  # For pingpong restore
        self.weights = network.get_weights()  # For pingpong restore
        logger.debug("Initialized %s", self.__class__.__name__)

    @property
    def output_shapes(self):
        """ Return the output shapes from the stored network """
        return [K.int_shape(output) for output in self.network.outputs]

    def set_name(self):
        """ Set the network name """
        name = self.type
        if self.side:
            name += "_{}".format(self.side)
        return name

    @property
    def output_names(self):
        """ Return output node names """
        output_names = [output.name for output in self.network.outputs]
        if self.is_output and not any(name.startswith("face_out") for name in output_names):
            # Saved models break if their layer names are changed, so dummy
            # in correct output names for legacy models
            output_names = self.get_output_names()
        return output_names

    def get_output_names(self):
        """ Return the output names based on number of channels and instances """
        output_types = ["mask_out" if K.int_shape(output)[-1] == 1 else "face_out"
                        for output in self.network.outputs]
        output_names = ["{}{}".format(name,
                                      "" if output_types.count(name) == 1 else "_{}".format(idx))
                        for idx, name in enumerate(output_types)]
        logger.debug("Overridden output_names: %s", output_names)
        return output_names

    def load(self, fullpath=None):
        """ Load model """
        fullpath = fullpath if fullpath else self.filename
        logger.debug("Loading model: '%s'", fullpath)
        try:
            network = load_model(self.filename, custom_objects=get_custom_objects())
        except ValueError as err:
            if str(err).lower().startswith("cannot create group in read only mode"):
                self.convert_legacy_weights()
                return True
            logger.warning("Failed loading existing training data. Generating new models")
            logger.debug("Exception: %s", str(err))
            return False
        except OSError as err:  # pylint: disable=broad-except
            logger.warning("Failed loading existing training data. Generating new models")
            logger.debug("Exception: %s", str(err))
            return False
        self.config = network.get_config()
        self.network = network  # Update network with saved model
        if get_backend() == "amd":
            self.network.name = self.name
        else:
            # No setter in tensorflow.keras
            self.network._name = self.name  # pylint:disable=protected-access
        return True

    def save(self, fullpath=None, backup_func=None):
        """ Save model """
        fullpath = fullpath if fullpath else self.filename
        if backup_func:
            backup_func(fullpath)
        logger.debug("Saving model: '%s'", fullpath)
        self.weights = self.network.get_weights()
        self.network.save(fullpath)

    def convert_legacy_weights(self):
        """ Convert legacy weights files to hold the model topology """
        logger.info("Adding model topology to legacy weights file: '%s'", self.filename)
        self.network.load_weights(self.filename)
        self.save(backup_func=None)
        if get_backend() == "amd":
            self.network.name = self.type
        else:
            # No setter in tensorflow.keras
            self.network._name = self.type  # pylint:disable=protected-access


class State():
    """ Class to hold the model's current state and autoencoder structure """
    def __init__(self, model_dir, model_name, config_changeable_items,
                 no_logs, training_image_size):
        logger.debug("Initializing %s: (model_dir: '%s', model_name: '%s', "
                     "config_changeable_items: '%s', no_logs: %s, "
                     "training_image_size: '%s'", self.__class__.__name__, model_dir, model_name,
                     config_changeable_items, no_logs, training_image_size)
        self.serializer = get_serializer("json")
        filename = "{}_state.{}".format(model_name, self.serializer.file_extension)
        self.filename = os.path.join(model_dir, filename)
        self.name = model_name
        self.iterations = 0
        self.session_iterations = 0
        self.training_size = training_image_size
        self.sessions = dict()
        self.lowest_avg_loss = dict()
        self.inputs = dict()
        self.config = dict()
        self.load(config_changeable_items)
        self.session_id = self.new_session_id()
        self.create_new_session(no_logs, config_changeable_items)
        logger.debug("Initialized %s:", self.__class__.__name__)

    @property
    def face_shapes(self):
        """ Return a list of stored face shape inputs """
        return [tuple(val) for key, val in self.inputs.items() if key.startswith("face")]

    @property
    def mask_shapes(self):
        """ Return a list of stored mask shape inputs """
        return [tuple(val) for key, val in self.inputs.items() if key.startswith("mask")]

    @property
    def loss_names(self):
        """ Return the loss names for this session """
        return self.sessions[self.session_id]["loss_names"]

    @property
    def current_session(self):
        """ Return the current session dict """
        return self.sessions[self.session_id]

    @property
    def first_run(self):
        """ Return True if this is the first run else False """
        return self.session_id == 1

    def new_session_id(self):
        """ Return new session_id """
        if not self.sessions:
            session_id = 1
        else:
            session_id = max(int(key) for key in self.sessions.keys()) + 1
        logger.debug(session_id)
        return session_id

    def create_new_session(self, no_logs, config_changeable_items):
        """ Create a new session """
        logger.debug("Creating new session. id: %s", self.session_id)
        self.sessions[self.session_id] = {"timestamp": time.time(),
                                          "no_logs": no_logs,
                                          "loss_names": dict(),
                                          "batchsize": 0,
                                          "iterations": 0,
                                          "config": config_changeable_items}

    def add_session_loss_names(self, side, loss_names):
        """ Add the session loss names to the sessions dictionary """
        logger.debug("Adding session loss_names. (side: '%s', loss_names: %s", side, loss_names)
        self.sessions[self.session_id]["loss_names"][side] = loss_names

    def add_session_batchsize(self, batchsize):
        """ Add the session batchsize to the sessions dictionary """
        logger.debug("Adding session batchsize: %s", batchsize)
        self.sessions[self.session_id]["batchsize"] = batchsize

    def increment_iterations(self):
        """ Increment total and session iterations """
        self.iterations += 1
        self.sessions[self.session_id]["iterations"] += 1

    def load(self, config_changeable_items):
        """ Load state file """
        logger.debug("Loading State")
        if not os.path.exists(self.filename):
            logger.info("No existing state file found. Generating.")
            return
        state = self.serializer.load(self.filename)
        self.name = state.get("name", self.name)
        self.sessions = state.get("sessions", dict())
        self.lowest_avg_loss = state.get("lowest_avg_loss", dict())
        self.iterations = state.get("iterations", 0)
        self.training_size = state.get("training_size", 256)
        self.inputs = state.get("inputs", dict())
        self.config = state.get("config", dict())
        logger.debug("Loaded state: %s", state)
        self.replace_config(config_changeable_items)

    def save(self, backup_func=None):
        """ Save iteration number to state file """
        logger.debug("Saving State")
        if backup_func:
            backup_func(self.filename)
        state = {"name": self.name,
                 "sessions": self.sessions,
                 "lowest_avg_loss": self.lowest_avg_loss,
                 "iterations": self.iterations,
                 "inputs": self.inputs,
                 "training_size": self.training_size,
                 "config": _CONFIG}
        self.serializer.save(self.filename, state)
        logger.debug("Saved State")

    def replace_config(self, config_changeable_items):
        """ Replace the loaded config with the one contained within the state file
            Check for any fixed=False parameters changes and log info changes
        """
        global _CONFIG  # pylint: disable=global-statement
        legacy_update = self._update_legacy_config()
        # Add any new items to state config for legacy purposes
        for key, val in _CONFIG.items():
            if key not in self.config.keys():
                logger.info("Adding new config item to state file: '%s': '%s'", key, val)
                self.config[key] = val
        self.update_changed_config_items(config_changeable_items)
        logger.debug("Replacing config. Old config: %s", _CONFIG)
        _CONFIG = self.config
        if legacy_update:
            self.save()
        logger.debug("Replaced config. New config: %s", _CONFIG)
        logger.info("Using configuration saved in state file")

    def _update_legacy_config(self):
        """ Legacy updates for new config additions.

        When new config items are added to the Faceswap code, existing model state files need to be
        updated to handle these new items.

        Current existing legacy update items:

            * loss - If old `dssim_loss` is ``true`` set new `loss_function` to `ssim` otherwise
            set it to `mae`. Remove old `dssim_loss` item

            * masks - If `learn_mask` does not exist then it is set to ``True`` if `mask_type` is
            not ``None`` otherwise it is set to ``False``.

            * masks type - Replace removed masks 'dfl_full' and 'facehull' with `components` mask

        Returns
        -------
        bool
            ``True`` if legacy items exist and state file has been updated, otherwise ``False``
        """
        logger.debug("Checking for legacy state file update")
        priors = ["dssim_loss", "mask_type", "mask_type"]
        new_items = ["loss_function", "learn_mask", "mask_type"]
        updated = False
        for old, new in zip(priors, new_items):
            if old not in self.config:
                logger.debug("Legacy item '%s' not in config. Skipping update", old)
                continue

            # dssim_loss > loss_function
            if old == "dssim_loss":
                self.config[new] = "ssim" if self.config[old] else "mae"
                del self.config[old]
                updated = True
                logger.info("Updated config from legacy dssim format. New config loss "
                            "function: '%s'", self.config[new])
                continue

            # Add learn mask option and set to True if model has "penalized_mask_loss" specified
            if old == "mask_type" and new == "learn_mask" and new not in self.config:
                self.config[new] = self.config["mask_type"] is not None
                updated = True
                logger.info("Added new 'learn_mask' config item for this model. Value set to: %s",
                            self.config[new])
                continue

            # Replace removed masks with most similar equivalent
            if old == "mask_type" and new == "mask_type" and self.config[old] in ("facehull",
                                                                                  "dfl_full"):
                old_mask = self.config[old]
                self.config[new] = "components"
                updated = True
                logger.info("Updated 'mask_type' from '%s' to '%s' for this model",
                            old_mask, self.config[new])

        logger.debug("State file updated for legacy config: %s", updated)
        return updated

    def update_changed_config_items(self, config_changeable_items):
        """ Update any parameters which are not fixed and have been changed """
        if not config_changeable_items:
            logger.debug("No changeable parameters have been updated")
            return
        for key, val in config_changeable_items.items():
            old_val = self.config[key]
            if old_val == val:
                continue
            self.config[key] = val
            logger.info("Config item: '%s' has been updated from '%s' to '%s'", key, old_val, val)
