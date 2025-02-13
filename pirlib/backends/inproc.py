import argparse
import importlib
import pandas
import pathlib
import tempfile
from typing import Any, Dict, Optional

import pirlib.pir
import pirlib.iotypes
from pirlib.backends import Backend
from pirlib.iotypes import DirectoryPath, FilePath
from pirlib.utils import find_by_name


class InprocBackend(Backend):
    def execute(
        self,
        package: pirlib.pir.Package,
        graph_name: str,
        config: Optional[dict] = None,
        args: Optional[argparse.Namespace] = None,
        *,  # Keyword-only arguments below.
        inputs: Optional[Dict[str, Any]] = None,
    ) -> None:
        graph = package.flatten_graph(graph_name, validate=True)
        inputs = {} if inputs is None else inputs
        if args is not None:
            for spec in args.input:
                inp = find_by_name(graph.inputs, spec.name)
                if inp.iotype == "DIRECTORY":
                    inputs[spec.name] = DirectoryPath(spec.url.path)
                elif inp.iotype == "FILE":
                    inputs[spec.name] = FilePath(spec.url.path)
                elif inp.iotype == "DATAFRAME":
                    if spec.fmt == "csv":
                        inputs.iotype = pandas.read_csv(spec.url.path)
        # Validate all required inputs are provided.
        for inp in graph.inputs:
            if inp.name not in inputs:
                raise ValueError(f"missing input '{inp.name}'")
        # Execute nodes one at a time.
        node_outputs = {}
        while True:
            remaining_nodes = [
                node for node in graph.nodes if node.name not in node_outputs
            ]
            if not remaining_nodes:
                break
            # Find a node that has all inputs ready.
            for node in remaining_nodes:
                node_inputs = {}
                for inp in node.inputs:
                    if inp.source.graph_input is not None:
                        node_inputs[inp.name] = inputs[inp.source.graph_input]
                    if inp.source.node is not None:
                        if inp.source.node not in node_outputs:
                            break
                        if inp.source.output not in node_outputs[inp.source.node]:
                            break
                        node_inputs[inp.name] = node_outputs[inp.source.node][
                            inp.source.output
                        ]
                else:
                    break
            else:
                raise RuntimeError("could not finish execution")
            # Execute node and collect its outputs.
            node_outputs[node.name] = self._execute_node(node, node_inputs)
        outputs = {}
        for out in graph.outputs:
            if out.source.node is not None:
                outputs[out.name] = node_outputs[out.source.node][out.source.output]
            if out.source.graph_input is not None:
                outputs[out.name] = inputs[out.source.graph_input]
        if args is not None:
            for spec in args.output:
                out = find_by_name(graph.outputs, spec.name)
                if out.iotype == "DATAFRAME":
                    outputs[spec.name].to_csv(spec.url.path)
        return outputs

    def _execute_node(self, node: pirlib.pir.Node, inputs: Dict[str, Any]):
        module_name, handler_name = node.entrypoint.handler.split(":")
        handler = getattr(importlib.import_module(module_name), handler_name)
        outputs = {}
        for out in node.outputs:
            if out.iotype == "DIRECTORY":
                outputs[out.name] = DirectoryPath(tempfile.mkdtemp())
            elif out.iotype == "FILE":
                outputs[out.name] = FilePath(tempfile.mkstemp()[1])
            else:
                outputs[out.name] = None
        handler.run_handler(node, inputs, outputs)
        return outputs
