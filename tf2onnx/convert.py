# SPDX-License-Identifier: Apache-2.0


"""
python -m tf2onnx.convert : api and commandline tool to convert a tensorflow model to onnx
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

# pylint: disable=unused-argument,unused-import,ungrouped-imports,wrong-import-position

import argparse
import os
import sys
from distutils.version import LooseVersion

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import tensorflow as tf

from tf2onnx.tfonnx import process_tf_graph
from tf2onnx import constants, logging, utils, optimizer
from tf2onnx import tf_loader
from tf2onnx.graph import ExternalTensorStorage
from tf2onnx.tf_utils import compress_graph_def

# pylint: disable=unused-argument

_HELP_TEXT = """
Usage Examples:

python -m tf2onnx.convert --saved-model saved_model_dir --output model.onnx
python -m tf2onnx.convert --input frozen_graph.pb  --inputs X:0 --outputs output:0 --output model.onnx
python -m tf2onnx.convert --checkpoint checkpoint.meta  --inputs X:0 --outputs output:0 --output model.onnx

For help and additional information see:
    https://github.com/onnx/tensorflow-onnx

If you run into issues, open an issue here:
    https://github.com/onnx/tensorflow-onnx/issues
"""


def get_args():
    """Parse commandline."""
    parser = argparse.ArgumentParser(description="Convert tensorflow graphs to ONNX.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=_HELP_TEXT)
    parser.add_argument("--input", help="input from graphdef")
    parser.add_argument("--graphdef", help="input from graphdef")
    parser.add_argument("--saved-model", help="input from saved model")
    parser.add_argument("--tag", help="tag to use for saved_model")
    parser.add_argument("--signature_def", help="signature_def from saved_model to use")
    parser.add_argument("--concrete_function", type=int, default=None,
                        help="For TF2.x saved_model, index of func signature in __call__ (--signature_def is ignored)")
    parser.add_argument("--checkpoint", help="input from checkpoint")
    parser.add_argument("--keras", help="input from keras model")
    parser.add_argument("--tflite", help="input from tflite model")
    parser.add_argument("--large_model", help="use the large model format (for models > 2GB)", action="store_true")
    parser.add_argument("--output", help="output model file")
    parser.add_argument("--inputs", help="model input_names")
    parser.add_argument("--outputs", help="model output_names")
    parser.add_argument("--ignore_default", help="comma-separated list of names of PlaceholderWithDefault "
                                                 "ops to change into Placeholder ops")
    parser.add_argument("--use_default", help="comma-separated list of names of PlaceholderWithDefault ops to "
                                              "change into Identity ops using their default value")
    parser.add_argument("--opset", type=int, default=None, help="opset version to use for onnx domain")
    parser.add_argument("--dequantize", help="Remove quantization from model. Only supported for tflite currently.",
                        action="store_true")
    parser.add_argument("--custom-ops", help="comma-separated map of custom ops to domains in format OpName:domain")
    parser.add_argument("--extra_opset", default=None,
                        help="extra opset with format like domain:version, e.g. com.microsoft:1")
    parser.add_argument("--target", default=",".join(constants.DEFAULT_TARGET), choices=constants.POSSIBLE_TARGETS,
                        help="target platform")
    parser.add_argument("--continue_on_error", help="continue_on_error", action="store_true")
    parser.add_argument("--verbose", "-v", help="verbose output, option is additive", action="count")
    parser.add_argument("--debug", help="debug mode", action="store_true")
    parser.add_argument("--output_frozen_graph", help="output frozen tf graph to file")
    parser.add_argument("--fold_const", help="Deprecated. Constant folding is always enabled.",
                        action="store_true")
    # experimental
    parser.add_argument("--inputs-as-nchw", help="transpose inputs as from nhwc to nchw")
    args = parser.parse_args()

    args.shape_override = None
    if args.input:
        # for backward compativility
        args.graphdef = args.input
    if args.graphdef or args.checkpoint:
        if not args.input and not args.outputs:
            parser.error("graphdef and checkpoint models need to provide inputs and outputs")
    if not any([args.graphdef, args.checkpoint, args.saved_model, args.keras, args.tflite]):
        parser.print_help()
        sys.exit(1)
    if args.inputs:
        args.inputs, args.shape_override = utils.split_nodename_and_shape(args.inputs)
    if args.outputs:
        args.outputs = args.outputs.split(",")
    if args.ignore_default:
        args.ignore_default = args.ignore_default.split(",")
    if args.use_default:
        args.use_default = args.use_default.split(",")
    if args.inputs_as_nchw:
        args.inputs_as_nchw = args.inputs_as_nchw.split(",")
    if args.target:
        args.target = args.target.split(",")
    if args.signature_def:
        args.signature_def = [args.signature_def]
    if args.dequantize:
        if not args.tflite:
            parser.error("dequantize flag is currently only supported for tflite")
    if args.extra_opset:
        tokens = args.extra_opset.split(':')
        if len(tokens) != 2:
            parser.error("invalid extra_opset argument")
        args.extra_opset = [utils.make_opsetid(tokens[0], int(tokens[1]))]

    return args


def make_default_custom_op_handler(domain):
    def default_custom_op_handler(ctx, node, name, args):
        node.domain = domain
        return node
    return default_custom_op_handler


def _convert_common(frozen_graph, name="unknown", input_names=None, output_names=None, initialized_tables=None,
                    opset=None, custom_ops=None, custom_op_handlers=None, custom_rewriter=None, ignore_default=False,
                    continue_on_error=False,  inputs_as_nchw=None, extra_opset=None, shape_override=None,
                    target=None, tensors_to_rename=None, large_model=False, use_default=False, dequantize=False,
                  tflite_path=None, output_frozen_graph=None, output_path=None):
    """Common processing for conversion."""

    model_proto = None
    external_tensor_storage = None
    const_node_values = None

    with tf.device("/cpu:0"):
        with tf.Graph().as_default() as tf_graph:
            if large_model:
                const_node_values = compress_graph_def(frozen_graph)
                external_tensor_storage = ExternalTensorStorage()
            if output_frozen_graph:
                utils.save_protobuf(output_frozen_graph, frozen_graph)
            tf.import_graph_def(frozen_graph, name='')
            g = process_tf_graph(tf_graph,
                                continue_on_error=True,
                                target=target,
                                opset=opset,
                                custom_op_handlers=custom_ops,
                                extra_opset=extra_opset,
                                shape_override=shape_override,
                                input_names=input_names,
                                output_names=output_names,
                                inputs_as_nchw=inputs_as_nchw,
                                const_node_values=const_node_values,
                                tensors_to_rename=tensors_to_rename,
                                use_default=use_default,
                                dequantize=dequantize,
                                ignore_default=False,
                                tflite_path=tflite_path,
                                initialized_tables=initialized_tables)
            onnx_graph = optimizer.optimize_graph(g)
            model_proto = onnx_graph.make_model("converted from {}".format(name),
                                                external_tensor_storage=external_tensor_storage)
        if output_path:
            if large_model:
                utils.save_onnx_zip(output_path, model_proto, external_tensor_storage)
            else:
                utils.save_protobuf(output_path, model_proto)

    return model_proto, external_tensor_storage


def main():
    args = get_args()
    logging.basicConfig(level=logging.get_verbosity_level(args.verbose))
    if args.debug:
        utils.set_debug_mode(True)

    logger = logging.getLogger(constants.TF2ONNX_PACKAGE_NAME)

    extra_opset = args.extra_opset or []
    tflite_path = None
    custom_ops = {}
    initialized_tables = None
    if args.custom_ops:
        using_tf_opset = False
        for op in args.custom_ops.split(","):
            if ":" in op:
                op, domain = op.split(":")
            else:
                # default custom ops for tensorflow-onnx are in the "tf" namespace
                using_tf_opset = True
                domain = constants.TENSORFLOW_OPSET.domain
            custom_ops[op] = (make_default_custom_op_handler(domain), [])
        if using_tf_opset:
            extra_opset.append(constants.TENSORFLOW_OPSET)

    if any(opset.domain == constants.CONTRIB_OPS_DOMAIN for opset in extra_opset):
        try:
            import tensorflow_text   # pylint: disable=import-outside-toplevel
        except ModuleNotFoundError:
            logger.warning("tensorflow_text not installed. Model will fail to load if tensorflow_text ops are used.")

    # get the frozen tensorflow model from graphdef, checkpoint or saved_model.
    if args.graphdef:
        graph_def, inputs, outputs = tf_loader.from_graphdef(args.graphdef, args.inputs, args.outputs)
        model_path = args.graphdef
    if args.checkpoint:
        graph_def, inputs, outputs = tf_loader.from_checkpoint(args.checkpoint, args.inputs, args.outputs)
        model_path = args.checkpoint
    if args.saved_model:
        graph_def, inputs, outputs, initialized_tables = tf_loader.from_saved_model(
            args.saved_model, args.inputs, args.outputs, args.tag,
            args.signature_def, args.concrete_function, args.large_model, return_initialized_tables=True)
        model_path = args.saved_model
    if args.keras:
        graph_def, inputs, outputs = tf_loader.from_keras(
            args.keras, args.inputs, args.outputs)
        model_path = args.keras
    if args.tflite:
        graph_def = None
        inputs = None
        outputs = None
        tflite_path = args.tflite
        model_path = tflite_path

    if args.verbose:
        logger.info("inputs: %s", inputs)
        logger.info("outputs: %s", outputs)

    _, _ = _convert_common(
                        graph_def,
                        name=model_path,
                        continue_on_error=args.continue_on_error,
                        target=args.target,
                        opset=args.opset,
                        custom_op_handlers=custom_ops,
                        extra_opset=extra_opset,
                        shape_override=args.shape_override,
                        input_names=inputs,
                        output_names=outputs,
                        inputs_as_nchw=args.inputs_as_nchw,
                        large_model=args.large_model,
                        tensors_to_rename=None,
                        ignore_default=args.ignore_default,
                        use_default=args.use_default,
                        tflite_path=tflite_path,
                        dequantize=args.dequantize,
                        initialized_tables=initialized_tables,
                        output_path=args.output)

    # write onnx graph
    logger.info("")
    logger.info("Successfully converted TensorFlow model %s to ONNX", model_path)
    if args.output:
        if args.large_model:
            logger.info("Zipped ONNX model is saved at %s. Unzip before opening in onnxruntime.", args.output)
        else:
            logger.info("ONNX model is saved at %s", args.output)
    else:
        logger.info("To export ONNX model to file, please run with `--output` option")


def from_keras(model, input_signature=None, opset=None, custom_ops=None, custom_op_handlers=None,
               custom_rewriter=None, inputs_as_nchw=None, extra_opset=None, shape_override=None,
               target=None, large_model=False, output_path=None):
    """Returns a ONNX model_proto for a tf.keras model.

    Args:
        model: the tf.keras model we want to convert
        input_signature: a tf.TensorSpec or a numpy array defining the shape/dtype of the input
        opset: the opset to be used for the ONNX model, default is the latest
        target: list of workarounds applied to help certain platforms
        custom_op_handlers: dictionary of custom ops handlers
        custom_rewriter: list of custom graph rewriters
        extra_opset: list of extra opset's, for example the opset's used by custom ops
        shape_override: dict with inputs that override the shapes given by tensorflow
        inputs_as_nchw: transpose inputs in list from nchw to nhwc
        large_model: use the ONNX external tensor storage format
        output_path: save model to output_path

    Returns:
        An ONNX model_proto and an external_tensor_storage dict.
    """
    if LooseVersion(tf.__version__) < "1.15":
        raise NotImplementedError("from_keras requires tf-1.15 or newer")

    from tensorflow.python.keras.saving import saving_utils as _saving_utils # pylint: disable=import-outside-toplevel

    # let tensorflow do the checking if model is a valid model
    function = _saving_utils.trace_model_call(model, input_signature)
    concrete_func = function.get_concrete_function(*input_signature)

    input_names = [input_tensor.name for input_tensor in concrete_func.inputs
                    if input_tensor.dtype != tf.dtypes.resource]
    output_names = [output_tensor.name for output_tensor in concrete_func.outputs
                    if output_tensor.dtype != tf.dtypes.resource]

    initialized_tables = None

    tensors_to_rename = {v.name: k for k, v in concrete_func.structured_outputs.items()}
    for k in input_names:
        tensors_to_rename[k] = k.replace(":0", "")
    tensors_to_rename = None
    frozen_graph = tf_loader.from_function(concrete_func, input_names, output_names)
    model_proto, external_tensor_storage = _convert_common(frozen_graph,
                        name=model.name,
                        continue_on_error=True,
                        target=None,
                        opset=opset,
                        custom_op_handlers=custom_ops,
                        extra_opset=extra_opset,
                        shape_override=shape_override,
                        input_names=input_names,
                        output_names=output_names,
                        inputs_as_nchw=inputs_as_nchw,
                        large_model=large_model,
                        tensors_to_rename=tensors_to_rename,
                        initialized_tables=initialized_tables,
                        output_path=output_path)

    return model_proto, external_tensor_storage


def from_function(function, input_signature=None, opset=None, custom_ops=None, custom_op_handlers=None,
                  custom_rewriter=None, inputs_as_nchw=None, extra_opset=None, shape_override=None, target=None,
                  large_model=False, output_path=None):
    """Returns a ONNX model_proto for a tf.function.

    Args:
        function: the tf.function we want to convert
        input_signature: a tf.TensorSpec or a numpy array defining the shape/dtype of the input
        opset: the opset to be used for the ONNX model, default is the latest
        target: list of workarounds applied to help certain platforms
        custom_op_handlers: dictionary of custom ops handlers
        custom_rewriter: list of custom graph rewriters
        extra_opset: list of extra opset's, for example the opset's used by custom ops
        shape_override: dict with inputs that override the shapes given by tensorflow
        inputs_as_nchw: transpose inputs in list from nchw to nhwc
        large_model: use the ONNX external tensor storage format
        output_path: save model to output_path

    Returns:
        An ONNX model_proto and an external_tensor_storage dict.
    """
    if LooseVersion(tf.__version__) < "1.15":
        raise NotImplementedError("from_keras requires tf-1.15 or newer")

    concrete_func = function.get_concrete_function(*input_signature)

    input_names = [input_tensor.name for input_tensor in concrete_func.inputs
                    if input_tensor.dtype != tf.dtypes.resource]
    output_names = [output_tensor.name for output_tensor in concrete_func.outputs
                    if output_tensor.dtype != tf.dtypes.resource]

    initialized_tables = None
    tensors_to_rename = None
    frozen_graph = tf_loader.from_function(concrete_func, input_names, output_names)
    model_proto, external_tensor_storage = _convert_common(frozen_graph,
                        name=concrete_func.name,
                        continue_on_error=True,
                        target=None,
                        opset=opset,
                        custom_op_handlers=custom_ops,
                        extra_opset=extra_opset,
                        shape_override=shape_override,
                        input_names=input_names,
                        output_names=output_names,
                        inputs_as_nchw=inputs_as_nchw,
                        large_model=large_model,
                        tensors_to_rename=tensors_to_rename,
                        initialized_tables=initialized_tables,
                        output_path=output_path)

    return model_proto, external_tensor_storage


def from_graph(graph_def, name=None, input_names=None, output_names=None, opset=None, custom_ops=None,
               custom_op_handlers=None, custom_rewriter=None, inputs_as_nchw=None, extra_opset=None,
               shape_override=None, target=None, large_model=False, output_path=None):
    """Returns a ONNX model_proto for a tensorflow graphdef.

    Args:
        graphdef: the graphdef we want to convert
        input_names: list of input names
        output_names: list of output names
        name: A name for the graph
        opset: the opset to be used for the ONNX model, default is the latest
        target: list of workarounds applied to help certain platforms
        custom_op_handlers: dictionary of custom ops handlers
        custom_rewriter: list of custom graph rewriters
        extra_opset: list of extra opset's, for example the opset's used by custom ops
        shape_override: dict with inputs that override the shapes given by tensorflow
        inputs_as_nchw: transpose inputs in list from nchw to nhwc
        large_model: use the ONNX external tensor storage format
        output_path: save model to output_path

    Returns:
        An ONNX model_proto and an external_tensor_storage dict.
    """
    if not input_names:
        raise ValueError("input_names needs to be provided")
    if not output_names:
        raise ValueError("output_names needs to be provided")
    if not name:
        name = "unknown"
    initialized_tables = None
    tensors_to_rename = None

    with tf.device("/cpu:0"):
        with tf.Graph().as_default() as tf_graph:
            with tf_loader.tf_session(graph=tf_graph) as sess:
                tf.import_graph_def(graph_def, name='')
                frozen_graph = tf_loader.freeze_session(sess, input_names=input_names, output_names=output_names)
                input_names = tf_loader.inputs_without_resource(sess, input_names)
                frozen_graph = tf_loader.tf_optimize(input_names, output_names, graph_def)

    model_proto, external_tensor_storage = _convert_common(frozen_graph,
                        name=name,
                        continue_on_error=True,
                        target=None,
                        opset=opset,
                        custom_op_handlers=custom_ops,
                        extra_opset=extra_opset,
                        shape_override=shape_override,
                        input_names=input_names,
                        output_names=output_names,
                        inputs_as_nchw=inputs_as_nchw,
                        large_model=large_model,
                        tensors_to_rename=tensors_to_rename,
                        initialized_tables=initialized_tables,
                        output_path=output_path)

    return model_proto, external_tensor_storage


if __name__ == "__main__":
    main()
