"""
This module contains dataclasses that define the PIR (Pipeline Intermediate
Representation) format. The top-level contaner is the :obj:`Package`, which consists
of one or more :obj:`Graph` objects. Each graph is a directed acyclic graph (DAG) of
:obj:`Node` or :obj:`Subgraph` elements. Each node represents some executable procedure
with well-defined inputs and outputs, and the graph structure defines the dependencies
between those inputs and outputs. A graph can also embed subgraphs which are other
graphs in the same package. A visual example of a package is shown below.

.. image:: ../_static/img/pir-diagram.svg
"""
from __future__ import annotations

import copy
import re
import typeguard
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pirlib.utils import find_by_name


@dataclass
class Package:
    """
    This dataclass encodes a package containing multiple graphs. Graphs in the same
    package can embed each other as subgraphs, as long as they are not recursively
    nested.

    :ivar graphs: List of graphs in this package, must all have unique names.
    """
    graphs: List[Graph] = field(default_factory=list)

    def flatten_graph(self, graph_name: str, validate: bool = True) -> Graph:
        """
        Return a graph from this package after flattening all its subgraphs. The
        resulting graph should only contain nodes and no subgraphs. Each node in a
        subgraph is converted into a node in the parent graph with a new node name
        equal to ``<subgraph name>.<node name>``.

        :param graph_name: Name of the graph in this package to flatten.
        :param validate: Validate the resulting flattened graph. Validation may fail,
                for example, if the resulting graph has nodes with conflicting names.
        :return: The resulting flattened graph.
        """
        graph = copy.deepcopy(find_by_name(self.graphs, graph_name))
        if graph is None:
            raise ValueError(f"graph with name '{graph_name}' not found in package")
        for subgraph in graph.subgraphs:
            g = self.flatten_graph(subgraph.graph, validate=False)
            # Add prefix to subgraph nodes names.
            for n in g.nodes:
                n.name = f"{subgraph.name}.{n.name}"
                for i in n.inputs:
                    if i.source.node is not None:
                        i.source.node = f"{subgraph.name}.{i.source.node}"
            for o in g.outputs:
                if o.source.node is not None:
                    o.source.node = f"{subgraph.name}.{o.source.node}"
            # Merge subgraph into main graph.
            for node in graph.nodes:
                for inp in node.inputs:
                    if inp.source.subgraph == subgraph.name:
                        for o in g.outputs:
                            if o.name == inp.source.output:
                                inp.source = o.source
            for out in graph.outputs:
                if out.source.subgraph == subgraph.name:
                    for o in g.outputs:
                        if o.name == out.source.output:
                            out.source = o.source
            for n in g.nodes:
                for i in n.inputs:
                    if i.source.graph_input is not None:
                        for si in subgraph.inputs:
                            if i.source.graph_input == si.name:
                                i.source = si.source
            graph.nodes.extend(g.nodes)
        graph.subgraphs = []
        if validate:
            graph.validate()
        return graph

    def validate(self):
        """
        Validate this package and all graphs contained within it. A package is valid if
        (1) all of its graphs are valid and have unique names, (2) all subgraphs are
        valid references to another graph in the same package, and (3) there are no
        recursively nested subgraphs.

        :raises ValidationError: If the package is invalid.
        """
        _validate_names(self.graphs, "graph")
        for graph in self.graphs:
            try:
                graph.validate()
            except ValidationError as err:
                raise ValidationError(f"graph '{graph.name}': {err}") from None
            for subgraph in graph.subgraphs:
                self._validate_subgraph(subgraph)
            if self._is_recursive(graph, []):
                raise ValidationError(f"package contains recursive subgraphs")

    def _validate_subgraph(self, subgraph):
        graph = find_by_name(self.graphs, subgraph.graph)
        if graph is None:
            raise ValidationError(
                f"subgraph '{subgraph.name}' refers to missing graph '{subgraph.graph}'"
            )
        for inp in subgraph.inputs:
            g_inp = find_by_name(graph.inputs, inp.name)
            if g_inp is None:
                raise ValidationError(
                    f"subgraph '{subgraph.name}' input '{inp.name}' "
                    f"could not be found in graph '{subgraph.graph}'"
                )
            if g_inp.iotype != inp.iotype:
                raise ValidationError(
                    f"subgraph '{subgraph.name}' input '{inp.name}' iotype "
                    f"'{inp.iotype}' does not match graph '{subgraph.graph}' "
                    f"input iotype '{g_inp.iotype}'"
                )
        for out in subgraph.outputs:
            g_out = find_by_name(graph.outputs, out.name)
            if g_out is None:
                raise ValidationError(
                    f"subgraph '{subgraph.name}' output '{out.name}' "
                    f"could not be found in graph '{subgraph.graph}'"
                )
            if g_out.iotype != out.iotype:
                raise ValidationError(
                    f"subgraph '{subgraph.name}' output '{out.name}' iotype "
                    f"'{out.iotype}' does not match graph '{subgraph.graph}' "
                    f"output iotype '{g_out.iotype}'"
                )

    def _is_recursive(self, graph: Graph, visited: List[str]):
        if graph.name in visited:
            return True
        visited = visited + [graph.name]
        for subgraph in graph.subgraphs:
            if self._is_recursive(find_by_name(self.graphs, subgraph.graph), visited):
                return True
        return False


@dataclass
class Graph:
    """
    This dataclass encodes a directed acyclic graph (DAG) of nodes each with well
    defined inputs and outputs. The graph itself also has inputs and outputs, where its
    inputs are placeholders for values to be provided when the graph is executed. Each
    graph can also embed as subgraphs any other graph in the same package.

    :ivar name: Name of the graph, must be unique among all graphs in a package.
    :ivar nodes: The nodes contained in the graph. Node names must be unique among all
            nodes and subgraphs in the graph.
    :ivar subgraphs: The subgraphs contained in the graph. Subgraph names must be unique
            among all nodes and subgraphs in the graph.
    :ivar inputs: The expected inputs for the graph, must all have unique names.
    :ivar outputs: The outputs for the graph, must all have unique names. Each graph
            output must have the same iotype as its source.
    """
    name: str
    nodes: List[Node] = field(default_factory=list)
    subgraphs: List[Subgraph] = field(default_factory=list)
    inputs: List[GraphInput] = field(default_factory=list)
    outputs: List[GraphOutput] = field(default_factory=list)

    def validate(self):
        """
        Validate this graph. A graph is valid if (1) all of its nodes, subgraphs,
        inputs, and outputs are valid, (2) all node/subgraph inputs and graph outputs
        have valid sources, and (3) there are no cycles in the graph.

        :raises ValidationError: If the graph is invalid.
        """
        _validate_fields(self)
        for node in self.nodes:
            try:
                node.validate()
            except ValidationError as err:
                raise ValidationError(f"node '{node.name}': {err}") from None
        for subgraph in self.subgraphs:
            try:
                subgraph.validate()
            except ValidationError as err:
                raise ValidationError(f"subgraph '{subgraph.name}': {err}") from None
        _validate_names(self.nodes + self.subgraphs, "node or subgraph")
        for inp in self.inputs:
            try:
                inp.validate()
            except ValidationError as err:
                raise ValidationError(f"graph input '{inp.name}': {err}") from None
        _validate_names(self.inputs, "graph input")
        for out in self.outputs:
            try:
                out.validate()
            except ValidationError as err:
                raise ValidationError(f"graph output '{out.name}': {err}") from None
        _validate_names(self.outputs, "graph output")
        self._validate_connectivity()
        self._validate_acyclicity()

    def _validate_connectivity(self):
        for out in self.outputs:
            try:
                self._validate_source(out.source, out.iotype)
            except ValidationError as err:
                raise ValidationError(f"graph output '{out.name}': {err}") from None
        for node in self.nodes:
            for inp in node.inputs:
                try:
                    self._validate_source(inp.source, inp.iotype)
                except ValidationError as err:
                    raise ValidationError(
                        f"node '{node.name}': input '{inp.name}': {err}"
                    ) from None
        for subgraph in self.subgraphs:
            for inp in subgraph.inputs:
                try:
                    self._validate_source(inp.source, inp.iotype)
                except ValidationError as err:
                    raise ValidationError(
                        f"subgraph '{subgraph.name}': input '{inp.name}': {err}"
                    ) from None

    def _validate_source(self, source, iotype):
        if source.graph_input is not None:
            graph_input = find_by_name(self.inputs, source.graph_input)
            if graph_input is None:
                raise ValidationError(
                    f"reference to missing graph input '{source.graph_input}'"
                )
            source_iotype = graph_input.iotype
        elif source.node is not None:
            node = find_by_name(self.nodes, source.node)
            if node is None:
                raise ValidationError(f"reference to missing node '{source.node}'")
            output = find_by_name(node.outputs, source.output)
            if output is None:
                raise ValidationError(
                    f"reference to missing output '{source.output}' of node "
                    f"'{node.name}'"
                )
            source_iotype = output.iotype
        elif source.subgraph is not None:
            subgraph = find_by_name(self.subgraphs, source.subgraph)
            if subgraph is None:
                raise ValidationError(
                    f"reference to missing subgraph '{source.subgraph}'"
                )
            output = find_by_name(subgraph.outputs, source.output)
            if output is None:
                raise ValidationError(
                    f"reference to missing output '{source.output}' of subgraph "
                    f"'{subgraph.name}'"
                )
            source_iotype = output.iotype
        if source_iotype != iotype:
            raise ValidationError(
                f"iotype '{iotype}' differs from source iotype '{source_iotype}'"
            )

    def _validate_acyclicity(self):
        visited_node_names = set()
        visited_subgraph_names = set()
        for root in self.nodes + self.subgraphs:
            stack = [root]
            while stack:
                item = stack.pop()
                if isinstance(item, Node):
                    if item.name in visited_node_names:
                        continue
                    visited_node_names.add(item.name)
                elif isinstance(item, Subgraph):
                    if item.name in visited_subgraph_names:
                        continue
                    visited_subgraph_names.add(item.name)
                for inp in item.inputs:
                    if inp.source.node is not None:
                        name = inp.source.node
                        node = find_by_name(self.nodes, name)
                        if node == root:
                            raise ValidationError(
                                f"cycle detected containing node '{name}'"
                            )
                        stack.append(node)
                    elif inp.source.subgraph is not None:
                        name = inp.source.subgraph
                        subgraph = find_by_name(self.subgraphs, name)
                        if subgraph == root:
                            raise ValidationError(
                                f"cycle detected containing subgraph '{name}'"
                            )
                        stack.append(subgraph)


@dataclass
class GraphInput:
    """
    This dataclass encodes an input of a graph. A graph input represents a
    "placeholder" for a user-provided input value, and so does not have a connected
    source.

    :ivar name: Name of the input, must be unique among all inputs of the graph.
    :ivar iotype: Expected type of the input.
    """
    name: str
    iotype: str

    def validate(self):
        _validate_fields(self)


@dataclass
class GraphOutput:
    """
    This dataclass encodes an output of a graph. Since a graph itself does not perform
    any computation, a graph output simply refers to an output of a node or subgraph, or
    an input of the same graph.

    :ivar name: Name of the graph output, must be unique among all outputs of the graph.
    :ivar iotype: Type of the graph output, must be equal to the type of the source.
    :ivar source: The source of the graph output.
    """
    name: str
    iotype: str
    source: DataSource

    def validate(self):
        _validate_fields(self)
        self.source.validate()


@dataclass
class Subgraph:
    """
    This dataclass encodes a subgraph embedded in a graph. A subgraph can refer to any
    other graph in the same package as the parent graph. Each of the subgraph's inputs
    and outputs must correspond to (i.e. have the same name and iotype as) an input or
    output of the embedded graph. The configs of the subgraph override the configs of
    the embedded graph.

    :ivar name: Name of the subgraph. Must be unique among all nodes and subgraphs in a
            valid graph.
    :ivar graph: Name of the graph to be embedded as a subgraph. Must be in the same
            package as the parent graph.
    :ivar config: Config values that override the embedded graph's configs.
    :ivar inputs: Expected inputs for this subgraph, must all have unique names. Each
            input must have the same name and iotype as an input of the embedded graph.
    :ivar outputs: Expected outputs for this subgraph, must all have unique names. Each
            output must have the same name and iotype as an output of the embedded
            graph.
    """
    name: str
    graph: str
    config: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    inputs: List[Input] = field(default_factory=list)
    outputs: List[Output] = field(default_factory=list)

    def validate(self):
        _validate_fields(self)
        for inp in self.inputs:
            try:
                inp.validate()
            except ValidationError as err:
                raise ValidationError(f"input '{inp.name}': {err}") from None
        _validate_names(self.inputs, "input")
        for out in self.outputs:
            try:
                out.validate()
            except ValidationError as err:
                raise ValidationError(f"output '{out.name}': {err}") from None
        _validate_names(self.outputs, "output")


@dataclass
class Node:
    """
    This dataclass encodes a node in a graph. A node represents a procedure that can be
    executed on several inputs to produce several outputs. All node inputs must have
    unique names, and all node outputs also must have unique names.

    :ivar name: Name of the node. Must be unique among all nodes and subgraphs in a
            valid graph.
    :ivar entrypoint: Entrypoint to the code executed by this node.
    :ivar framework: Execution framework for this node.
    :ivar config: Configuration values which are passed down to the procedure executed
            by this node. Can be any json or yaml serializable mapping.
    :ivar inputs: Expected inputs for this node, must all have unique names.
    :ivar outputs: Expected outputs for this node, must all have unique names.
    """
    name: str
    entrypoint: Entrypoint
    framework: Optional[Framework] = None
    config: Dict[str, Any] = field(default_factory=dict)
    inputs: List[Input] = field(default_factory=list)
    outputs: List[Output] = field(default_factory=list)

    def validate(self):
        _validate_fields(self)
        for inp in self.inputs:
            try:
                inp.validate()
            except ValidationError as err:
                raise ValidationError(f"input '{inp.name}': {err}") from None
        _validate_names(self.inputs, "input")
        for out in self.outputs:
            try:
                out.validate()
            except ValidationError as err:
                raise ValidationError(f"output '{out.name}': {err}") from None
        _validate_names(self.outputs, "output")
        try:
            self.entrypoint.validate()
        except ValidationError as err:
            raise ValidationError(f"entrypoint: {err}") from None
        if self.framework is not None:
            try:
                self.framework.validate()
            except ValidationError as err:
                raise ValidationError(f"framework: {err}") from None


@dataclass
class Input:
    """
    This dataclass encodes a named input of a node or subgraph with a connected source.
    If it is the input of a subgraph, then its name must be equal to a name of some
    graph input of that subgraph.

    :ivar name: Name of the input. Must be unique among all inputs in a valid node or
            subgraph.
    :ivar iotype: Expected type of the input. In a valid graph, must be equal to the
            iotype of the source.
    :ivar source: Source of the input. Can be a graph input or a node/subgraph output.
    """
    name: str
    iotype: str
    source: DataSource

    def validate(self):
        _validate_fields(self)
        try:
            self.source.validate()
        except ValidationError as err:
            raise ValidationError(f"source: {err}") from None


@dataclass
class Output:
    """
    This dataclass encodes a named output of a node or a subgraph. An output can be an
    input source for other downstream nodes or subgraphs within the same graph, or be
    an output of the graph itself.

    :ivar name: Name of the output. Must be unique among all outputs in a valid node or
            subgraph.
    :ivar iotype: Type of the output.
    """
    name: str
    iotype: str

    def validate(self):
        _validate_fields(self)


@dataclass
class Framework:
    """
    This dataclass encodes the execution framework and configuration for a node.

    :ivar name: Name of the framework used for executing a node.
    :ivar config: Framework configuration for executing a node.
    """
    name: str
    config: Dict[str, Any] = field(default_factory=dict)

    def validate(self):
        _validate_fields(self)


@dataclass
class Entrypoint:
    """
    This dataclass encodes the entrypoint for executing a node. An entrypoint is
    typically a reference to a function or procedure in code (called "handler").

    :ivar version: The API version for the handler.
    :ivar handler: Reference to the handler, format depends on the runtime. For python
            runtimes, expects ``<module>.<name>``, where ``<module>`` is the fully
            qualified name of the module containing the handler, and ``<name>`` is the
            name of the handler object within that module.
    :ivar runtime: Identifier of the handler's runtime, e.g. ``"python:3.8"``.
    :ivar codeurl: Optional URL of the code used to run the handler. ``None`` means any
            and all handler code can be found in the local environment or docker image.
    :ivar image: Optional name of the Docker image used to run the handler. ``None``
            means the handler can be run in the local environment.
    """
    version: str
    handler: str
    runtime: str
    codeurl: Optional[str] = None
    image: Optional[str] = None

    def validate(self):
        _validate_fields(self)


@dataclass
class DataSource:
    """
    This dataclass encodes a reference to the source of an intermediate value in a PIR
    graph. A source can either be (1) an output of a node in the graph, (2) an output
    of a subgraph of the graph, or (3) an input of the graph itself. Exactly one of
    ``node``, ``subgraph``, or ``graph_input`` must be provided.

    :ivar node: Name of a node if the source is the output of that node.
    :ivar subgraph: Name of a subgraph if the source is the output of that subgraph.
    :ivar output: Name of the output of a node or subgraph if the source is the output
            of that node or subgraph.
    :ivar graph_input: Name of the graph input if the source is an input of the graph.
    """
    node: Optional[str] = None
    subgraph: Optional[str] = None
    output: Optional[str] = None
    graph_input: Optional[str] = None

    def validate(self):
        _validate_fields(self)
        count = sum(
            [
                self.node is not None,
                self.subgraph is not None,
                self.graph_input is not None,
            ]
        )
        if count != 1:
            raise ValidationError(
                "exactly one of 'node', 'subgraph', or 'graph_input' is expected"
            )
        if self.node is not None or self.subgraph is not None:
            if self.output is None:
                raise ValidationError(
                    "'output' is required if either 'node' or 'subgraph' is provided"
                )


class ValidationError(ValueError):
    """
    Exception raised when any part of a PIR package fails validation.
    """

    def __init__(self, message):
        super().__init__(message)


def _validate_fields(instance):
    classname = instance.__class__.__name__
    for name, hint in instance.__annotations__.items():
        value = getattr(instance, name)
        try:
            typeguard.check_type(f"{classname}.{name}", value, hint)
        except TypeError as err:
            raise ValidationError(err.message) from None


def _validate_names(items: Any, label: str) -> None:
    once = set()
    twice = set()
    for item in items:
        if item.name not in once:
            once.add(item.name)
        else:
            twice.add(item.name)
    if twice:
        text = ", ".join(repr(name) for name in twice)
        raise ValidationError(f"duplicate {label} name(s): {text}")
