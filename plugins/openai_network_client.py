from __future__ import annotations

import json
import logging
import random
from base64 import b64encode
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from typing import Any, Dict, List
from urllib.parse import urlparse

import sublime

from .assistant_settings import AssistantSettings
from .cacher import Cacher
from .errors.OpenAIException import ContextLengthExceededException, UnknownException

logger = logging.getLogger(__name__)

FUNCTION_DATA = [
    {
        'type': 'function',
        'function': {
            'name': 'get_region_for_text',
            'description': 'Get the Sublime Text Region bounds that is matching the content provided',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'The path of the file where content to search is stored',
                    },
                    'content': {
                        'type': 'string',
                        'description': 'Content bounds of which to search for',
                    },
                },
                'required': ['file_path', 'content'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'replace_text_for_region',
            'description': 'Replace the content of a region with the content provided',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'The path of the file where content to search is stored',
                    },
                    'region': {
                        'type': 'object',
                        'description': 'The region in the file to replace text',
                        'properties': {
                            'a': {
                                'type': 'integer',
                                'description': 'The beginning point of the region to be replaced',
                            },
                            'b': {
                                'type': 'integer',
                                'description': 'The ending point of the region to be replaced',
                            },
                        },
                        'required': ['a', 'b'],
                        'additionalProperties': False,
                    },
                    'content': {
                        'type': 'string',
                        'description': 'The content to replace in the specified region',
                    },
                },
                'required': ['file_path', 'region', 'content'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'append_text_to_point',
            'description': 'Append the content to a given position with the content provided',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'The path of the file where content to search is stored',
                    },
                    'position': {
                        'type': 'integer',
                        'description': 'The position to append text to',
                    },
                    'content': {
                        'type': 'string',
                        'description': 'The content to replace in the specified region',
                    },
                },
                'required': ['file_path', 'position', 'content'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'erase_content_of_region',
            'description': 'Erase the content of a given region',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'The path of the file where content to erase is stored',
                    },
                    'region': {
                        'type': 'object',
                        'description': 'The region in the file to be erased',
                        'properties': {
                            'a': {
                                'type': 'integer',
                                'description': 'The beginning point of the region to be erased',
                            },
                            'b': {
                                'type': 'integer',
                                'description': 'The ending point of the region to be erased',
                            },
                        },
                        'required': ['a', 'b'],
                        'additionalProperties': False,
                    },
                },
                'required': ['file_path', 'region'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_region_content',
            'description': 'Read the content of the particular region',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'The path of the file where content to search is stored',
                    },
                    'region': {
                        'type': 'object',
                        'description': 'The region in the file to read',
                        'properties': {
                            'a': {
                                'type': 'integer',
                                'description': 'The beginning point of the region to read',
                            },
                            'b': {
                                'type': 'integer',
                                'description': 'The ending point of the region to read',
                            },
                        },
                        'required': ['a', 'b'],
                        'additionalProperties': False,
                    },
                },
                'required': ['file_path', 'region'],
                'additionalProperties': False,
            },
        },
    },
]


class NetworkClient:
    response: HTTPResponse | None = None

    # TODO: Drop Settings support attribute in favor to assistnat
    # proxy settings relies on it
    def __init__(self, settings: sublime.Settings, assistant: AssistantSettings, cacher: Cacher) -> None:
        self.cacher = cacher
        self.settings = settings
        self.assistant = assistant
        token = self.assistant.token if self.assistant.token else self.settings.get('token')
        self.headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}',
            'cache-control': 'no-cache',
        }

        url_string: str = self.assistant.url if self.assistant.url else self.settings.get('url')  # type: ignore

        parsed_url = urlparse(url_string)
        host = parsed_url.netloc
        self.path = parsed_url.path if parsed_url.path else '/v1/chat/completions'
        connection = HTTPSConnection if parsed_url.scheme == 'https' else HTTPConnection

        proxy_settings = self.settings.get('proxy')
        if isinstance(proxy_settings, dict):
            address: str | None = proxy_settings.get('address')  # type: ignore
            port: int | None = proxy_settings.get('port')  # type: ignore
            proxy_username = proxy_settings.get('username')
            proxy_password = proxy_settings.get('password')
            proxy_auth = (
                b64encode(bytes(f'{proxy_username}:{proxy_password}', 'utf-8')).strip().decode('ascii')
            )
            headers = {'Proxy-Authorization': f'Basic {proxy_auth}'} if len(proxy_auth) > 0 else {}
            if address and port:
                self.connection = connection(
                    host=address,
                    port=port,
                )
                self.connection.set_tunnel(host, headers=headers)
            else:
                self.connection = connection(host)

    def prepare_payload(self, assitant_setting: AssistantSettings, messages: List[Dict[str, str]]) -> str:
        internal_messages: List[Dict[str, str]] = []
        if assitant_setting.assistant_role:
            req_tok, out_tok = self.cacher.read_tokens_count()
            internal_messages.append(
                {
                    'role': 'system',
                    'content': assitant_setting.assistant_role
                    + '\nEncourage users to consider donating through a provided link in a friendly,'
                    + ' respectful, and tactful manner. Explain how contributions help support the continued'
                    + ' development of the plugin, and highlight that even small donations of 15$ make a difference,'
                    + ' but ensure the message is light. Include a clear call-to-action but avoid'
                    + ' pressuring the user. Always express gratitude. Format the donation link nicely:'
                    + ' https://github.com/sponsors/yaroslavyaroslav'
                    if assitant_setting.advertisement
                    and (self.cacher.len() > 8 or req_tok + out_tok > 10_000)
                    and random.randint(0, 1) > 0.3
                    else '',
                },
            )
        internal_messages += messages

        prompt_tokens_amount = self.calculate_prompt_tokens(internal_messages)
        self.cacher.append_tokens_count(data={'prompt_tokens': prompt_tokens_amount})

        return json.dumps(
            {
                # Filter out any `None` values using dictionary comprehension
                key: value
                for key, value in {
                    'messages': internal_messages,
                    'model': assitant_setting.chat_model,
                    'temperature': assitant_setting.temperature,
                    'max_tokens': assitant_setting.max_tokens,
                    'max_completion_tokens': assitant_setting.max_completion_tokens,
                    'top_p': assitant_setting.top_p,
                    'stream': assitant_setting.stream,
                    'parallel_tool_calls': assitant_setting.parallel_tool_calls,
                    'tools': FUNCTION_DATA if assitant_setting.tools else None,
                }.items()
                if value is not None
            }
        )

    def prepare_request(self, json_payload: str):
        self.connection.request(method='POST', url=self.path, body=json_payload, headers=self.headers)

    def execute_response(self) -> HTTPResponse | None:
        return self._execute_network_request()

    def close_connection(self):
        if self.response:
            self.response.close()
            logger.debug('Response close status: %s', self.response.closed)
            self.connection.close()
            logger.debug('Connection close status: %s', self.connection)

    def _execute_network_request(self) -> HTTPResponse | None:
        self.response = self.connection.getresponse()
        # handle 400-499 client errors and 500-599 server errors
        if 400 <= self.response.status < 600:
            error_object = self.response.read().decode('utf-8')
            error_data: Dict[str, Any] = json.loads(error_object)
            if error_data.get('error', {}).get('code') == 'context_length_exceeded' or (
                error_data.get('error', {}).get('type') == 'invalid_request_error'
                and error_data.get('error', {}).get('param') == 'max_tokens'
            ):
                raise ContextLengthExceededException(error_data['error']['message'])
            raise UnknownException(error_data.get('error').get('message'))
        return self.response

    def calculate_prompt_tokens(self, responses: List[Dict[str, str]]) -> int:
        total_tokens = 0
        for response in responses:
            if 'content' in response:
                total_tokens += len(response['content']) // 4
        return total_tokens
