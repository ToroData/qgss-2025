"""
This module implements and extends functions from the IBM Quantum tutorial:
    https://quantum.cloud.ibm.com/docs/en/tutorials/sample-based-quantum-diagonalization

Original functions and layout logic were adapted from that tutorial, particularly the
`get_zigzag_physical_layout`, `create_lucj_zigzag_layout`, and `lightweight_layout_error_scoring`
utilities, which are used to map a zigzag interaction pattern to IBM Quantum hardware.

Modifications in this version include:
    - Support for ancilla qubits via a `num_ancillas` parameter, which increases the total
      number of qubits requested in the mapping process.
    - Exclusion of unreliable physical qubits and couplings through the new parameters:
        * `bad_readout_qubits` (qubits with high measurement error),
        * `bad_czgate_edges` (couplings with poor CZ fidelity or known issues).
    - Improved layout filtering and scoring based on hardware error properties.
    - Code restructuring to make backend compatibility and customization more robust.

These extensions make the module suitable for noise-aware compilation and layout selection
on real QPUs while preserving the core structure of the zigzag mapping strategy.
"""
from typing import Sequence
 
import rustworkx
from qiskit.providers import BackendV2
from rustworkx import NoEdgeBetweenNodes, PyGraph
 
IBM_TWO_Q_GATES = {"cx", "ecr", "cz"}
 
 
def create_linear_chains(num_orbitals: int) -> PyGraph:
    """In zig-zag layout, there are two linear chains (with connecting qubits between
    the chains). This function creates those two linear chains: a rustworkx PyGraph
    with two disconnected linear chains. Each chain contains `num_orbitals` number
    of nodes, i.e., in the final graph there are `2 * num_orbitals` number of nodes.
 
    Args:
        num_orbitals (int): Number orbitals or nodes in each linear chain. They are
            also known as alpha-alpha interaction qubits.
 
    Returns:
        A rustworkx.PyGraph with two disconnected linear chains each with `num_orbitals`
            number of nodes.
    """
    G = rustworkx.PyGraph()
 
    for n in range(num_orbitals):
        G.add_node(n)
 
    for n in range(num_orbitals - 1):
        G.add_edge(n, n + 1, None)
 
    for n in range(num_orbitals, 2 * num_orbitals):
        G.add_node(n)
 
    for n in range(num_orbitals, 2 * num_orbitals - 1):
        G.add_edge(n, n + 1, None)
 
    return G
 
 
def create_lucj_zigzag_layout(
    num_orbitals: int, backend_coupling_graph: PyGraph
) -> tuple[PyGraph, int]:
    """This function creates the complete zigzag graph that 'can be mapped' to a IBM QPU with
    heavy-hex connectivity (the zigzag must be an isomorphic sub-graph to the QPU/backend
    coupling graph for it to be mapped).
    The zigzag pattern includes both linear chains (alpha-alpha interactions) and connecting
    qubits between the linear chains (alpha-beta interactions).
 
    Args:
        num_orbitals (int): Number of orbitals, i.e., number of nodes in each alpha-alpha linear chain.
        backend_coupling_graph (PyGraph): The coupling graph of the backend on which the LUCJ ansatz
            will be mapped and run. This function takes the coupling graph as a undirected
            `rustworkx.PyGraph` where there is only one 'undirected' edge between two nodes,
            i.e., qubits. Usually, the coupling graph of a IBM backend is directed (e.g., Eagle devices
            such as ibm_sherbrooke) or may have two edges between two nodes (e.g., Heron `ibm_torino`).
            A user needs to be make such graphs undirected and/or remove duplicate edges to make them
            compatible with this function.
 
    Returns:
        G_new (PyGraph): The graph with IBM backend compliant zigzag pattern.
        num_alpha_beta_qubits (int): Number of connecting qubits between the linear chains
            in the zigzag pattern. While we want as many connecting (alpha-beta) qubits between
            the linear (alpha-alpha) chains, we cannot accommodate all due to qubit and connectivity
            constraints of backends. This is the maximum number of connecting qubits the zigzag pattern
            can have while being backend compliant (i.e., isomorphic to backend coupling graph).
    """
    isomorphic = False
    G = create_linear_chains(num_orbitals=num_orbitals)
 
    num_iters = num_orbitals
    while not isomorphic:
        G_new = G.copy()
        num_alpha_beta_qubits = 0
        for n in range(num_iters):
            if n % 4 == 0:
                new_node = 2 * num_orbitals + num_alpha_beta_qubits
                G_new.add_node(new_node)
                G_new.add_edge(n, new_node, None)
                G_new.add_edge(new_node, n + num_orbitals, None)
                num_alpha_beta_qubits = num_alpha_beta_qubits + 1
        isomorphic = rustworkx.is_subgraph_isomorphic(
            backend_coupling_graph, G_new
        )
        num_iters -= 1
 
    return G_new, num_alpha_beta_qubits
 
 
def lightweight_layout_error_scoring(
    backend: BackendV2,
    virtual_edges: Sequence[Sequence[int]],
    physical_layouts: Sequence[int],
    two_q_gate_name: str,
    bad_readout_qubits=[],
    bad_czgate_edges=[]
) -> list[list[list[int], float]]:
    """Lightweight and heuristic function to score isomorphic layouts. There can be many zigzag patterns,
    each with different set of physical qubits, that can be mapped to a backend. Some of them may
    include less noise qubits and couplings than others. This function computes a simple error score
    for each such layout. It sums up 2Q gate error for all couplings in the zigzag pattern (layout) and
    measurement of errors of physical qubits in the layout to compute the error score

    This version extends the original tutorial implementation in:
        https://quantum.cloud.ibm.com/docs/en/tutorials/sample-based-quantum-diagonalization

    Modifications:
        - Excludes any layout containing bad readout qubits (`bad_readout_qubits`) from scoring.
        - Skips layouts using CZ gate couplings listed in `bad_czgate_edges`, filtering them before scoring.
        - Uses both forward and reverse directions of edges for error lookup, to handle undirected coupling.
 
    Note:
        This lightweight scoring can be refined using concepts such as mapomatic.
 
    Args:
        backend (BackendV2): A backend.
        virtual_edges (Sequence[Sequence[int]]): Edges in the device compliant zigzag pattern where
            nodes are numbered from 0 to (2 * num_orbitals + num_alpha_beta_qubits).
        physical_layouts (Sequence[int]): All physical layouts of the zigzag pattern that are isomorphic
            to each other and to the larger backend coupling map.
        two_q_gate_name (str): The name of the two-qubit gate of the backend. The name is used for fetching
            two-qubit gate error from backend properties.
 
    Returns:
        scores (list): A list of lists where each sublist contains two items. First item is the layout, and
            second item is a float representing error score of the layout. The layouts in the `scores` are
            sorted in the ascending order of error score.
    """
    props = backend.properties()
    scores = []
    for layout in physical_layouts:
        if any(q in bad_readout_qubits for q in layout):
            continue
        
        skip = False
        for edge in virtual_edges:
            physical_edge = (layout[edge[0]], layout[edge[1]])
            if physical_edge in bad_czgate_edges or physical_edge[::-1] in bad_czgate_edges:
                skip = True
                break
        if skip:
            continue

        total_2q_error = 0
        for edge in virtual_edges:
            physical_edge = (layout[edge[0]], layout[edge[1]])
            try:
                ge = props.gate_error(two_q_gate_name, physical_edge)
            except Exception:
                ge = props.gate_error(two_q_gate_name, physical_edge[::-1])
            total_2q_error += ge

        total_measurement_error = 0
        for qubit in layout:
            meas_error = props.readout_error(qubit)
            total_measurement_error += meas_error

        scores.append([layout, total_2q_error + total_measurement_error])

    return sorted(scores, key=lambda x: x[1])
 
 
def _make_backend_cmap_pygraph(backend: BackendV2) -> PyGraph:
    graph = backend.coupling_map.graph
    if not graph.is_symmetric():
        graph.make_symmetric()
    backend_coupling_graph = graph.to_undirected()
 
    edge_list = backend_coupling_graph.edge_list()
    removed_edge = []
    for edge in edge_list:
        if set(edge) in removed_edge:
            continue
        try:
            backend_coupling_graph.remove_edge(edge[0], edge[1])
            removed_edge.append(set(edge))
        except NoEdgeBetweenNodes:
            pass
 
    return backend_coupling_graph
 
 
def get_zigzag_physical_layout(
    num_orbitals: int,
    backend: BackendV2,
    score_layouts: bool = True,
    num_ancillas=0,
    bad_readout_qubits=[],
    bad_czgate_edges=[]
) -> tuple[list[int], int]:
    """The main function that generates the zigzag pattern with physical qubits that can be used
    as an `intial_layout` in a preset passmanager/transpiler.

    This version extends the original tutorial implementation in:
        https://quantum.cloud.ibm.com/docs/en/tutorials/sample-based-quantum-diagonalization

    Modifications:
        - sum of ancillas to orbital number
 
    Args:
        num_orbitals (int): Number of orbitals.
        backend (BackendV2): A backend.
        score_layouts (bool): Optional. If `True`, it uses the `lightweight_layout_error_scoring`
            function to score the isomorphic layouts and returns the layout with less erroneous qubits.
            If `False`, returns the first isomorphic subgraph.
 
    Returns:
        A tuple of device compliant layout (list[int]) with zigzag pattern and an int representing
            number of alpha-beta-interactions.
    """
    backend_coupling_graph = _make_backend_cmap_pygraph(backend=backend)
 
    G, num_alpha_beta_qubits = create_lucj_zigzag_layout(
        num_orbitals=num_orbitals+num_ancillas,
        backend_coupling_graph=backend_coupling_graph,
    )
 
    isomorphic_mappings = rustworkx.vf2_mapping(
        backend_coupling_graph, G, subgraph=True
    )
    isomorphic_mappings = list(isomorphic_mappings)
 
    edges = list(G.edge_list())
 
    layouts = []
    for mapping in isomorphic_mappings:
        initial_layout = [None] * (2 * (num_orbitals+num_ancillas) + num_alpha_beta_qubits)
        for key, value in mapping.items():
            initial_layout[value] = key
        layouts.append(initial_layout)
 
    two_q_gate_name = IBM_TWO_Q_GATES.intersection(
        backend.configuration().basis_gates
    ).pop()
 
    if score_layouts:
        scores = lightweight_layout_error_scoring(
            backend=backend,
            virtual_edges=edges,
            physical_layouts=layouts,
            two_q_gate_name=two_q_gate_name,
            bad_readout_qubits=bad_readout_qubits,
            bad_czgate_edges=bad_czgate_edges
        )
 
        return scores[0][0][:-num_alpha_beta_qubits], num_alpha_beta_qubits
 
    return layouts[0][:-num_alpha_beta_qubits], num_alpha_beta_qubits
