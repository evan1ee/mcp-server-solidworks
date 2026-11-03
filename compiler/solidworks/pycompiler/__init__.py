"""pycompiler — deterministic Feature Graph IR compiler.

Lowers a CAD-neutral Feature Graph IR (cad-planner/contracts/feature-graph.schema.json)
into ordered calls to the execution/solidworks low-level tools, and resolves semantic and
geometric references against live geometry via ExecutionPort.

Architectural rules (see docs/05-compiler.md + master-architecture.md):
  - Deterministic. NO LLM, NO MCP, NO COM.
  - Reaches the execution layer ONLY through an injected ExecutionPort (plain REST behind it),
    never by importing the adapter, the MCP SDK, or httpx. Moving it into a standalone REST
    service is a transport swap behind ExecutionPort, with lowering / resolver / validation
    logic untouched (IR-ADR-003).
"""

from .compiler import CompileResult, compile_and_run  # noqa: F401
from .errors import FeatureError  # noqa: F401
from .execution_port import ExecutionPort  # noqa: F401
from .ir_schema import validate  # noqa: F401

__all__ = [
    "CompileResult",
    "ExecutionPort",
    "FeatureError",
    "compile_and_run",
    "validate",
]
