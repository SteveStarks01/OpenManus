from typing import Dict, List, Literal, Optional, Union
import httpx
import json
import asyncio

from openai import (
    APIError,
    AsyncAzureOpenAI,
    AsyncOpenAI,
    AuthenticationError,
    OpenAIError,
    RateLimitError,
)
from tenacity import retry, stop_after_attempt, wait_random_exponential

from app.config import LLMSettings, config
from app.logger import logger  # Assuming a logger is set up in your app
from app.schema import Message


class LLM:
    _instances: Dict[str, "LLM"] = {}

    def __new__(
        cls, config_name: str = "default", llm_config: Optional[LLMSettings] = None
    ):
        if config_name not in cls._instances:
            instance = super().__new__(cls)
            instance.__init__(config_name, llm_config)
            cls._instances[config_name] = instance
        return cls._instances[config_name]

    def __init__(
        self, config_name: str = "default", llm_config: Optional[LLMSettings] = None
    ):
        if not hasattr(self, "client"):  # Only initialize if not already initialized
            llm_config = llm_config or config.llm
            llm_config = llm_config.get(config_name, llm_config["default"])
            self.model = llm_config.model
            self.max_tokens = llm_config.max_tokens
            self.temperature = llm_config.temperature
            self.api_type = llm_config.api_type
            self.api_key = llm_config.api_key
            self.api_version = llm_config.api_version
            self.base_url = llm_config.base_url
            self.http_referer = llm_config.http_referer
            self.x_title = llm_config.x_title
            self.host = llm_config.host
            
            # Initialize client based on API type
            if self.api_type == "azure":
                self.client = AsyncAzureOpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    api_version=self.api_version,
                )
            elif self.api_type == "ollama":
                # For Ollama, we'll handle API calls directly with httpx
                self.client = None
            elif self.api_type == "openrouter":
                # For OpenRouter, we use OpenAI client with custom headers
                headers = {}
                if self.http_referer:
                    headers["HTTP-Referer"] = self.http_referer
                if self.x_title:
                    headers["X-Title"] = self.x_title
                
                self.client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    default_headers=headers
                )
            else:
                # Default to standard OpenAI client
                self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    @staticmethod
    def format_messages(messages: List[Union[dict, Message]]) -> List[dict]:
        """
        Format messages for LLM by converting them to OpenAI message format.

        Args:
            messages: List of messages that can be either dict or Message objects

        Returns:
            List[dict]: List of formatted messages in OpenAI format

        Raises:
            ValueError: If messages are invalid or missing required fields
            TypeError: If unsupported message types are provided

        Examples:
            >>> msgs = [
            ...     Message.system_message("You are a helpful assistant"),
            ...     {"role": "user", "content": "Hello"},
            ...     Message.user_message("How are you?")
            ... ]
            >>> formatted = LLM.format_messages(msgs)
        """
        formatted_messages = []

        for message in messages:
            if isinstance(message, dict):
                # If message is already a dict, ensure it has required fields
                if "role" not in message:
                    raise ValueError("Message dict must contain 'role' field")
                formatted_messages.append(message)
            elif isinstance(message, Message):
                # If message is a Message object, convert it to dict
                formatted_messages.append(message.to_dict())
            else:
                raise TypeError(f"Unsupported message type: {type(message)}")

        # Validate all messages have required fields
        for msg in formatted_messages:
            if msg["role"] not in ["system", "user", "assistant", "tool"]:
                raise ValueError(f"Invalid role: {msg['role']}")
            if "content" not in msg and "tool_calls" not in msg:
                raise ValueError(
                    "Message must contain either 'content' or 'tool_calls'"
                )

        return formatted_messages

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
    )
    async def ask(
        self,
        messages: List[Union[dict, Message]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        stream: bool = True,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send a prompt to the LLM and get the response.

        Args:
            messages: List of conversation messages
            system_msgs: Optional system messages to prepend
            stream (bool): Whether to stream the response
            temperature (float): Sampling temperature for the response

        Returns:
            str: The generated response

        Raises:
            ValueError: If messages are invalid or response is empty
            OpenAIError: If API call fails after retries
            Exception: For unexpected errors
        """
        try:
            # Format system and user messages
            if system_msgs:
                system_msgs = self.format_messages(system_msgs)
                messages = system_msgs + self.format_messages(messages)
            else:
                messages = self.format_messages(messages)

            # Handle Ollama API
            if self.api_type == "ollama":
                return await self._ask_ollama(messages, stream, temperature)

            # For OpenAI and OpenRouter (OpenAI compatible)
            if not stream:
                # Non-streaming request
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.max_tokens,
                    temperature=temperature or self.temperature,
                    stream=False,
                )
                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("Empty or invalid response from LLM")
                return response.choices[0].message.content

            # Streaming request
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=temperature or self.temperature,
                stream=True,
            )

            collected_messages = []
            async for chunk in response:
                chunk_message = chunk.choices[0].delta.content or ""
                collected_messages.append(chunk_message)
                print(chunk_message, end="", flush=True)

            print()  # Newline after streaming
            full_response = "".join(collected_messages).strip()
            if not full_response:
                raise ValueError("Empty response from streaming LLM")
            return full_response

        except ValueError as ve:
            logger.error(f"Validation error: {ve}")
            raise
        except OpenAIError as oe:
            logger.error(f"OpenAI API error: {oe}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in ask: {e}")
            raise
            
    async def _ask_ollama(
        self, 
        messages: List[dict],
        stream: bool = True,
        temperature: Optional[float] = None,
    ) -> str:
        """Handle Ollama API requests"""
        try:
            async with httpx.AsyncClient() as client:
                # Prepare the request payload
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "stream": stream,
                    "options": {
                        "temperature": temperature or self.temperature,
                        "num_predict": self.max_tokens,
                    }
                }
                
                endpoint = f"{self.host}/api/chat"
                
                if not stream:
                    # Non-streaming request
                    response = await client.post(endpoint, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    return data.get("message", {}).get("content", "")
                
                # Streaming request
                payload["stream"] = True
                collected_messages = []
                
                async with client.stream("POST", endpoint, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            if content:
                                collected_messages.append(content)
                                print(content, end="", flush=True)
                        except json.JSONDecodeError:
                            pass
                
                print()  # Newline after streaming
                full_response = "".join(collected_messages).strip()
                if not full_response:
                    raise ValueError("Empty response from Ollama")
                return full_response
                
        except httpx.HTTPError as e:
            logger.error(f"Ollama API error: {e}")
            raise ValueError(f"Error communicating with Ollama: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in Ollama request: {e}")
            raise

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
    )
    async def ask_tool(
        self,
        messages: List[Union[dict, Message]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        timeout: int = 60,
        tools: Optional[List[dict]] = None,
        tool_choice: Literal["none", "auto", "required"] = "auto",
        temperature: Optional[float] = None,
        **kwargs,
    ):
        """
        Ask LLM using functions/tools and return the response.

        Args:
            messages: List of conversation messages
            system_msgs: Optional system messages to prepend
            timeout: Request timeout in seconds
            tools: List of tools to use
            tool_choice: Tool choice strategy
            temperature: Sampling temperature for the response
            **kwargs: Additional completion arguments

        Returns:
            ChatCompletionMessage: The model's response

        Raises:
            ValueError: If tools, tool_choice, or messages are invalid
            OpenAIError: If API call fails after retries
            Exception: For unexpected errors
        """
        try:
            # Validate tool_choice
            if tool_choice not in ["none", "auto", "required"]:
                raise ValueError(f"Invalid tool_choice: {tool_choice}")

            # Format messages
            if system_msgs:
                system_msgs = self.format_messages(system_msgs)
                messages = system_msgs + self.format_messages(messages)
            else:
                messages = self.format_messages(messages)

            # Validate tools if provided
            if tools:
                for tool in tools:
                    if not isinstance(tool, dict) or "type" not in tool:
                        raise ValueError("Each tool must be a dict with 'type' field")
            
            # Handle Ollama (note: Ollama has limited native tool support)
            if self.api_type == "ollama":
                if tools:
                    logger.warning("Ollama has limited tool support; appending to system message")
                    # For Ollama, we'll add tools description to the system message
                    tools_desc = json.dumps(tools, indent=2)
                    tool_instructions = f"You have access to the following tools: {tools_desc}. " \
                                        f"When appropriate, use these tools by responding in the format " \
                                        f"'TOOL_CALL: {{\"name\": \"tool_name\", \"arguments\": {{\"arg1\": \"value\"}}}}'"
                    
                    # Find or create system message
                    found_system = False
                    for msg in messages:
                        if msg["role"] == "system":
                            msg["content"] = msg["content"] + "\n\n" + tool_instructions
                            found_system = True
                            break
                    
                    if not found_system:
                        messages.insert(0, {"role": "system", "content": tool_instructions})
                
                # Process with regular Ollama chat
                response_content = await self._ask_ollama(
                    messages=messages,
                    stream=False,
                    temperature=temperature,
                )
                
                # Create an OpenAI-like response object
                return {
                    "content": response_content,
                    "role": "assistant"
                }

            # Set up the completion request (for OpenAI and OpenRouter)
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature or self.temperature,
                max_tokens=self.max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                timeout=timeout,
                **kwargs,
            )

            # Check if response is valid
            if not response.choices or not response.choices[0].message:
                print(response)
                raise ValueError("Invalid or empty response from LLM")

            return response.choices[0].message

        except ValueError as ve:
            logger.error(f"Validation error in ask_tool: {ve}")
            raise
        except OpenAIError as oe:
            if isinstance(oe, AuthenticationError):
                logger.error("Authentication failed. Check API key.")
            elif isinstance(oe, RateLimitError):
                logger.error("Rate limit exceeded. Consider increasing retry attempts.")
            elif isinstance(oe, APIError):
                logger.error(f"API error: {oe}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in ask_tool: {e}")
            raise
