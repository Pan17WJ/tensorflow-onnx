# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.tf2onnx.onnx_opset.math
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging

from tf2onnx import constants

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("onnx_opset.common")

# pylint: disable=unused-argument,missing-docstring

class BroadcastOp:
    @classmethod
    def version_4(cls, ctx, node, **kwargs):
        """Elementwise Ops with broadcast flag."""
        shape0 = ctx.get_shape(node.input[0])
        shape1 = ctx.get_shape(node.input[1])
        if shape0 != shape1:
            node.set_attr("broadcast", 1)
            # this works around shortcomings in the broadcasting code
            # of caffe2 and winml/rs4.
            if ctx.is_target(constants.TARGET_RS4):
                # in rs4 mul and add do not support scalar correctly
                if not shape0:
                    if node.inputs[0].is_const():
                        shape0 = node.inputs[0].scalar_to_dim1()
                if not shape1:
                    if node.inputs[1].is_const():
                        shape1 = node.inputs[1].scalar_to_dim1()
            if shape0 and shape1 and len(shape0) < len(shape1) and node.type in ["Mul", "Add"]:
                tmp = node.input[0]
                node.input[0] = node.input[1]
                node.input[1] = tmp
        else:
            node.set_attr("broadcast", 0)

    @classmethod
    def version_7(cls, ctx, node, **kwargs):
        """Elementwise Ops with broadcast flag."""
        shape0 = ctx.get_shape(node.input[0])
        shape1 = ctx.get_shape(node.input[1])
        if shape0 != shape1:
            # this works around shortcomings in the broadcasting code
            # of caffe2 and winml/rs4.
            if ctx.is_target(constants.TARGET_RS4):
                # in rs4 mul and add do not support scalar correctly
                if not shape0:
                    if node.inputs[0].is_const():
                        shape0 = node.inputs[0].scalar_to_dim1()
                if not shape1:
                    if node.inputs[1].is_const():
                        shape1 = node.inputs[1].scalar_to_dim1()
            if shape0 and shape1 and len(shape0) < len(shape1) and node.type in ["Mul", "Add"]:
                tmp = node.input[0]
                node.input[0] = node.input[1]
                node.input[1] = tmp
