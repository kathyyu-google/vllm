from dataclasses import dataclass
from enum import Enum
from typing import List, Mapping, Optional, Union

from vllm.inputs import PromptInputs
from vllm.lora.request import LoRARequest
from vllm.outputs import RequestOutput
from vllm.prompt_adapter.request import PromptAdapterRequest
from vllm.sampling_params import SamplingParams

VLLM_RPC_SUCCESS_STR = "SUCCESS"

IPC_INPUT_EXT = "_input_socket"
IPC_OUTPUT_EXT = "_output_socket"
IPC_HEALTH_EXT = "_health_socket"
IPC_DATA_EXT = "_data_socket"


class MQEngineDeadError(RuntimeError):
    pass


@dataclass
class RPCGenerateRequest:
    inputs: PromptInputs
    sampling_params: SamplingParams
    request_id: str
    lora_request: Optional[LoRARequest] = None
    trace_headers: Optional[Mapping[str, str]] = None
    prompt_adapter_request: Optional[PromptAdapterRequest] = None


@dataclass
class RPCError:
    request_id: Optional[str]
    is_engine_errored: bool
    exception: BaseException


@dataclass
class RPCAbortRequest:
    request_id: str


class RPCHealthRequest:
    pass


class RPCStartupRequest(Enum):
    IS_SERVER_READY = 1
    CLIENT_IS_READY = 2


@dataclass
class RPCStartupResponse:
    tracing_enabled: bool


RPC_REQUEST_T = Union[RPCGenerateRequest, RPCAbortRequest, RPCHealthRequest,
                      RPCStartupRequest]

REQUEST_OUTPUTS_T = Union[List[RequestOutput], RPCError]


def ENGINE_DEAD_ERROR(error: BaseException) -> MQEngineDeadError:
    return MQEngineDeadError(
        "Engine loop is not running. Inspect the stacktrace to "
        f"find the original error {repr(error)}.")
