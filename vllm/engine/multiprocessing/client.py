import asyncio
import pickle
from contextlib import contextmanager, suppress
from typing import (Any, AsyncGenerator, Dict, Iterator, Mapping, Optional,
                    Union)

import cloudpickle
import zmq
import zmq.asyncio
from zmq import Frame  # type: ignore[attr-defined]
from zmq.asyncio import Socket

from vllm.config import (DecodingConfig, LoRAConfig, ModelConfig,
                         ParallelConfig, SchedulerConfig)
from vllm.engine.multiprocessing import (ENGINE_DEAD_ERROR, IPC_DATA_EXT,
                                         IPC_HEALTH_EXT, IPC_INPUT_EXT,
                                         IPC_OUTPUT_EXT, REQUEST_OUTPUTS_T,
                                         RPC_REQUEST_T, VLLM_RPC_SUCCESS_STR,
                                         RPCAbortRequest, RPCError,
                                         RPCGenerateRequest, RPCStartupRequest,
                                         RPCUtilityRequest)
from vllm.envs import VLLM_RPC_GET_DATA_TIMEOUT_MS
from vllm.inputs import PromptInputs
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.outputs import EmbeddingRequestOutput, RequestOutput
from vllm.prompt_adapter.request import PromptAdapterRequest
from vllm.sampling_params import SamplingParams
from vllm.transformers_utils.tokenizer_group import init_tokenizer_from_configs

logger = init_logger(__name__)


class MPClientClosedError(Exception):
    """Exception class raised when the client is used post-close.
    
    The client can be closed, which closes the ZMQ context. This normally
    happens on server shutdown. In some cases, methods like abort and 
    do_log_stats will still be called and then try to open a socket, which 
    causes a ZMQError and creates a huge stack trace.
    So, we throw this error such that we can suppress it.
    """


class MQLLMEngineClient:
    """A client wrapper for MQLLMEngine that conforms to the
    EngineClient protocol.

    MQLLMEngine and MQLLMEngineClient are intended to run in separate
    processes communicating via zeromq ipc sockets.

    The entrypoint to MQLLMEngineClient is through the generate()
    method. On generate() MQLLMEngine does three things:
        - Creates an asyncio output queue
        - Sends a RPCGenerateRequest to the MQLLMEngine via zmq
        - Pulls RequestOutputs from its queue and yields them

    MQLLMEngine runs two background loops:
        - output_loop: the output loop pulls List[RequestOutput]
            from the MQLLMEngine via zmq (each list is the output
            of one engine_step in the LLMEngine). It then parses
            the list and pushes individual request_outputs into
            the corresponding output_queue such that they can be
            consumed by the .generate() method.
        - health_loop: the health loop queries the health socket
            every N seconds, confirming the engine is healthy
    """

    def __init__(self, ipc_path: str):
        self.context = zmq.asyncio.Context()
        self._errored = False
        self.dead_error = ENGINE_DEAD_ERROR

        # Send RPCGenerateRequest to the MQLLMEngine.
        self.input_socket: Socket = self.context.socket(zmq.constants.PUSH)
        self.input_socket.connect(f"{ipc_path}{IPC_INPUT_EXT}")

        # Receive streams of RequestOutput from the MQLLMEngine.
        self.output_socket: Socket = self.context.socket(zmq.constants.PULL)
        self.output_socket.connect(f"{ipc_path}{IPC_OUTPUT_EXT}")

        # IPC path for ack of check_health requests.
        self.health_socket: Socket = self.context.socket(zmq.constants.PULL)
        self.health_socket.connect(f"{ipc_path}{IPC_HEALTH_EXT}")

        # IPC path for the data socket.
        self.data_ipc_path = f"{ipc_path}{IPC_DATA_EXT}"

        # Stream for each individual request.
        self.output_queues: Dict[str, asyncio.Queue] = {}
        self.output_loop = asyncio.create_task(self.run_output_handler_loop())

        # Loop to check health of the LLMEngine periodically.
        self.health_loop: Optional[asyncio.Task] = None

    @contextmanager
    def get_data_socket(self) -> Iterator[Socket]:
        socket = self.context.socket(zmq.constants.DEALER)
        try:
            socket.connect(self.data_ipc_path)
            yield socket
        finally:
            socket.close(linger=0)

    async def run_check_health_loop(self, timeout: int):
        """Background loop that continually probes the RPCServer for health.
        
        The loop sends CHECK_HEALTH requests to the INPUT_SOCKET, which
        the MQLLMEngine server is blocking on.

        The Server replies on the HEALTH_SOCKET (rather than on the 
        OUTPUT_SOCKET such that the messages are not intermingled with
        output streaming).
        """

        try:
            while True:
                if await self.health_socket.poll(timeout=timeout) == 0:
                    # Wakeup every N seconds and do a health probe.
                    await self._send_one_way_rpc_request(
                        RPCUtilityRequest.CHECK_HEALTH, self.input_socket)

                    # Wait for ack from the health socket.
                    await self._await_ack(error_message="Health check failed.",
                                          socket=self.health_socket)
                else:
                    # Server sent a health status message unprompted.
                    await self._check_success(
                        error_message="Health check failed.",
                        socket=self.health_socket)

                logger.debug("Health probe successful.")

        except asyncio.CancelledError:
            logger.debug("Shutting down MQLLMEngineClient check health loop.")

        except Exception as e:
            logger.exception(repr(e))
            self._errored = True

    async def run_output_handler_loop(self):
        """Get RequestOutputs from Engine and stream to request Queues"""

        try:
            while True:
                message: Frame = await self.output_socket.recv(copy=False)
                request_outputs: REQUEST_OUTPUTS_T = pickle.loads(
                    message.buffer)

                is_error = (isinstance(request_outputs, BaseException) or
                            isinstance(request_outputs, RPCError))
                            
                if is_error:
                    if isinstance(request_outputs, RPCError):
                        rpc_error: RPCError = request_outputs
                        request_id = rpc_error.request_id
                        if rpc_error.is_engine_errored:
                            self._errored = True
                        exception = rpc_error.exception
                    else:
                        # MPLLMEngine should always return an RPCError
                        # when an issue arises. If we are here, we are in a 
                        # bad state and should shut down the server.
                        error: BaseException = request_outputs
                        logger.warning(
                            f"Recieved raw Exception {error} rather than "
                            "RPCError from MPLLMEngine. This should never happen.")
                        self._errored = True
                        request_id = None
                        exception = error

                    if request_id is None:
                        for queue_i in tuple(self.output_queues.values()):
                            queue_i.put_nowait(exception)
                    else:
                        queue = self.output_queues.get(request_id)
                        if queue is not None:
                            queue.put_nowait(exception)
                else:
                    # Put each output into the appropriate steam.
                    for request_output in request_outputs:
                        queue = self.output_queues.get(
                            request_output.request_id)
                        if queue is not None:
                            queue.put_nowait(request_output)

        except asyncio.CancelledError:
            logger.debug("Shutting down MQLLMEngineClient output handler.")

    async def setup(self):
        """Setup the client before it starts sending server requests."""

        with self.get_data_socket() as socket:
            # Wait until server is ready.
            await self._wait_for_server_rpc(socket)

            # Get the configs.
            self.model_config = await self._get_model_config_rpc(socket)
            self.decoding_config = await self._get_decoding_config_rpc(socket)
            self.tracing_flag = await self._is_tracing_enabled_rpc(socket)

            # Create the tokenizer group.
            # TODO: refactor OAI server to avoid needing this info.
            self.tokenizer = init_tokenizer_from_configs(
                model_config=self.model_config,
                scheduler_config=(await
                                  self._get_scheduler_config_rpc(socket)),
                parallel_config=(await self._get_parallel_config_rpc(socket)),
                enable_lora=bool(await self._get_lora_config_rpc(socket)),
            )

            # Start health_loop.
            self.health_loop = asyncio.create_task(
                self.run_check_health_loop(
                    timeout=VLLM_RPC_GET_DATA_TIMEOUT_MS))

            # Notify MQLLMEngine client is ready to start sending requests.
            await self._notify_ready(socket)

    def close(self):
        """Destroy the ZeroMQ Context."""
        # Close all sockets associated with this context and
        # then terminate the context.
        self.output_socket.close()
        self.input_socket.close()
        self.health_socket.close()
        self.context.destroy(linger=0)

        # Cancel background tasks.
        if self.health_loop is not None:
            self.health_loop.cancel()
        self.output_loop.cancel()

    async def _send_get_data_rpc_request(self, request: RPCStartupRequest,
                                         expected_type: Any,
                                         error_message: str,
                                         socket: Socket) -> Any:
        """Send an RPC request that is expecting data back."""

        # Ping RPCServer with a request.
        await socket.send_multipart((cloudpickle.dumps(request), ), copy=False)

        # Make sure the server responds in time.
        if await socket.poll(timeout=VLLM_RPC_GET_DATA_TIMEOUT_MS) == 0:
            raise TimeoutError("RPCServer didn't reply within "
                               f"{VLLM_RPC_GET_DATA_TIMEOUT_MS} ms")

        # Await the data from the Server.
        frame = await socket.recv(copy=False)
        data = pickle.loads(frame.buffer)

        if isinstance(data, Exception):
            # Re-raise exceptions returned by the server
            raise data

        if not isinstance(data, expected_type):
            # LoRAConfig can be None.
            if expected_type == LoRAConfig and data is None:
                pass
            elif isinstance(data, Exception):
                logger.error(error_message)
                raise data
            else:
                raise ValueError(error_message)

        return data

    async def _send_one_way_rpc_request(self, request: RPC_REQUEST_T,
                                        socket: Socket):
        """Send one-way RPC request to trigger an action."""

        await socket.send_multipart((cloudpickle.dumps(request), ))

    async def _await_ack(self, error_message: str, socket: Socket):
        "Await acknowledgement that a request succeeded."

        if await socket.poll(timeout=VLLM_RPC_GET_DATA_TIMEOUT_MS) == 0:
            raise TimeoutError("MQLLMEngine didn't reply within "
                               f"{VLLM_RPC_GET_DATA_TIMEOUT_MS}ms")

        await self._check_success(error_message, socket)

    async def _check_success(self, error_message: str, socket: Socket):
        frame = await socket.recv(copy=False)
        response = pickle.loads(frame.buffer)

        if not isinstance(response, str) or response != VLLM_RPC_SUCCESS_STR:
            if isinstance(response, BaseException):
                logger.error(error_message)
                raise response
            raise ValueError(error_message)

    async def get_tokenizer(self, lora_request: LoRARequest):
        return await self.tokenizer.get_lora_tokenizer_async(lora_request)

    async def get_decoding_config(self) -> DecodingConfig:
        return self.decoding_config

    async def get_model_config(self) -> ModelConfig:
        return self.model_config

    async def is_tracing_enabled(self) -> bool:
        return self.tracing_flag

    async def _wait_for_server_rpc(self, socket: Socket):
        """Wait for the RPCServer to start up."""

        await self._send_one_way_rpc_request(
            request=RPCStartupRequest.IS_SERVER_READY, socket=socket)

        await self._await_ack(error_message="Unable to start RPC Server",
                              socket=socket)

    async def _notify_ready(self, socket: Socket):
        """Get the RPCServer that the RPCClient is ready"""

        await self._send_one_way_rpc_request(
            request=RPCStartupRequest.CLIENT_IS_READY, socket=socket)

    async def _get_model_config_rpc(self, socket: Socket) -> ModelConfig:
        """Get the ModelConfig object from the RPC Server"""

        return await self._send_get_data_rpc_request(
            RPCStartupRequest.GET_MODEL_CONFIG,
            expected_type=ModelConfig,
            error_message="Could not get ModelConfig from RPC Server",
            socket=socket)

    async def _get_decoding_config_rpc(self, socket: Socket) -> DecodingConfig:
        """Get DecodingConfig from the RPCServer"""

        return await self._send_get_data_rpc_request(
            RPCStartupRequest.GET_DECODING_CONFIG,
            expected_type=DecodingConfig,
            error_message="Could not get DecodingConfig from RPC Server",
            socket=socket)

    async def _get_parallel_config_rpc(self, socket: Socket) -> ParallelConfig:
        """Get ParallelConfig from the RPCServer"""

        return await self._send_get_data_rpc_request(
            RPCStartupRequest.GET_PARALLEL_CONFIG,
            expected_type=ParallelConfig,
            error_message="Could not get ParallelConfig from RPC Server",
            socket=socket)

    async def _get_scheduler_config_rpc(self,
                                        socket: Socket) -> SchedulerConfig:
        """Get SchedulerConfig from the RPCServer"""

        return await self._send_get_data_rpc_request(
            RPCStartupRequest.GET_SCHEDULER_CONFIG,
            expected_type=SchedulerConfig,
            error_message="Could not get SchedulerConfig from RPC Server",
            socket=socket)

    async def _get_lora_config_rpc(self, socket: Socket) -> LoRAConfig:
        """Get LoRAConfig from the RPCServer"""

        return await self._send_get_data_rpc_request(
            RPCStartupRequest.GET_LORA_CONFIG,
            expected_type=LoRAConfig,
            error_message="Could not get LoRAConfig from RPC Server",
            socket=socket)

    async def _is_tracing_enabled_rpc(self, socket: Socket) -> bool:
        """Get is_tracing_enabled flag from the RPCServer"""

        return await self._send_get_data_rpc_request(
            RPCStartupRequest.GET_TRACING_ENABLED,
            expected_type=bool,
            error_message="Could not get is_tracing_enabled from RPC Server",
            socket=socket)

    async def abort(self, request_id: str):
        """Send an ABORT_REQUEST signal to the RPC Server"""

        with suppress(MPClientClosedError):
            await self._send_one_way_rpc_request(
                request=RPCAbortRequest(request_id), socket=self.input_socket)

    async def do_log_stats(self):
        """Send a DO_LOG_STATS signal to the RPC Server"""
        with suppress(MPClientClosedError):
            await self._send_one_way_rpc_request(
                request=RPCUtilityRequest.DO_LOG_STATS,
                socket=self.input_socket)

    async def check_health(self):
        """
        The check health loop probes the health status of the
        Engine's health every N seconds and sets _errored if
        the engine is unhealth. So check_health just raises
        an ENGINE_DEAD_ERROR if we find self._errored
        """
        if self._errored:
            raise ENGINE_DEAD_ERROR

    @property
    def is_running(self) -> bool:
        return not self._errored

    @property
    def is_stopped(self) -> bool:
        return self._errored

    @property
    def errored(self) -> bool:
        return self._errored

    async def generate(
        self,
        inputs: PromptInputs,
        sampling_params: SamplingParams,
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
        trace_headers: Optional[Mapping[str, str]] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None
    ) -> AsyncGenerator[RequestOutput, None]:
        """Send an RPCGenerateRequest to the RPCServer and stream responses."""

        if self._errored:
            raise ENGINE_DEAD_ERROR

        # 1) Create output queue for this requests.
        queue: asyncio.Queue[Union[RequestOutput,
                                   BaseException]] = asyncio.Queue()
        self.output_queues[request_id] = queue

        try:
            # 2) Send the RPCGenerateRequest to the MQLLMEngine.
            await self.input_socket.send_multipart((cloudpickle.dumps(
                RPCGenerateRequest(
                    inputs=inputs,
                    sampling_params=sampling_params,
                    request_id=request_id,
                    lora_request=lora_request,
                    trace_headers=trace_headers,
                    prompt_adapter_request=prompt_adapter_request)), ))

            # 3) Stream the RequestOutputs from the output queue. Note
            # that the output_loop pushes RequestOutput objects to this
            # queue after pulling them from the zmq socket.
            finished = False
            while not finished:
                request_output = await queue.get()

                if isinstance(request_output, BaseException):
                    raise request_output

                finished = request_output.finished
                yield request_output

        finally:
            # TODO: check if excepted requests are getting here.
            self.output_queues.pop(request_id)

            # Request was canceled by the client.
            if not finished and not self._errored:
                await self.abort(request_id)

    async def encode(self, *args,
                     **kwargs) -> AsyncGenerator[EmbeddingRequestOutput, None]:
        raise NotImplementedError(
            "Embeddings not supported with multiprocessing backend")