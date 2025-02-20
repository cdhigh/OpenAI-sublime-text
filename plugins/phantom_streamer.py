from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List

import mdpopups
from sublime import (
    NewFileFlags,
    Phantom,
    PhantomLayout,
    PhantomSet,
    View,
    active_window,
    load_settings,
    set_clipboard,
    set_timeout,
)

from .cacher import Cacher
from .output_panel import SharedOutputPanelListener

VIEW_SETTINGS_KEY_OPENAI_TEXT = 'VIEW_SETTINGS_KEY_OPENAI_TEXT'
OPENAI_COMPLETION_KEY = 'openai_completion'
PHANTOM_TEMPLATE = (
    '---'
    + '\nallow_code_wrap: true'
    + '\n---'
    + '\n\n<a href="close">[x]</a> \
    | <a href="copy">Copy</a> \
    | <a href="append">Append</a> \
    | <a href="replace">Replace</a> \
    | <a href="new_file">In New Tab</a> \
    | <a href="history">Add to History</a>'
    + '\n\n{streaming_content}'
)
CLASS_NAME = 'openai-completion-phantom'

logger = logging.getLogger(__name__)


class PhantomStreamer:
    user_input: List[Dict[str, Any]] | List[Dict[str, str]]

    def __init__(self, view: View, cacher: Cacher) -> None:
        self.view = view
        self.cacher = cacher
        self.phantom_set = PhantomSet(self.view, OPENAI_COMPLETION_KEY)
        self.completion: str = ''
        self.phantom: Phantom | None = None
        self.phantom_id: int | None = None
        self.listner = SharedOutputPanelListener(markdown=True, cacher=self.cacher)
        self.is_discardable: bool = (
            load_settings('openAI.sublime-settings')
            .get('chat_presentation', {})
            .get('is_tabs_discardable', False)
        )
        if len(view.sel()) > 0:
            logger.debug(f'view selection: {view.sel()[0]}')
            self.selected_region = view.sel()[0]  # saving only first selection to ease buffer logic

    def update_completion(self, user_input: List[Dict[str, Any]] | List[Dict[str, str]], completion: str):
        line_beginning = self.view.line(self.view.sel()[0])
        self.completion += completion
        self.user_input = user_input

        content = PHANTOM_TEMPLATE.format(streaming_content=self.completion)
        html = mdpopups._create_html(self.view, content, wrapper_class=CLASS_NAME)

        phantom = (
            self.phantom
            if self.phantom
            else Phantom(line_beginning, html, PhantomLayout.BLOCK, self.close_phantom)
        )

        def update_main_thread():
            # Updating on the main thread
            self.phantom_set.update([phantom])

        # Switch to the main thread to update phantoms
        set_timeout(update_main_thread)

    def close_phantom(self, attribute):
        logger.debug(f'attribure: `{attribute}`')
        if attribute in [action.value for action in PhantomActions]:
            if attribute == PhantomActions.copy.value:
                set_clipboard(self.completion)
            if attribute == PhantomActions.append.value:
                self.view.run_command(
                    'text_stream_at',
                    {'position': self.selected_region.end(), 'text': self.completion},
                )
            elif attribute == PhantomActions.replace.value:
                region_object = {
                    'a': self.selected_region.begin(),
                    'b': self.selected_region.end(),
                }
                self.view.run_command('replace_region', {'region': region_object, 'text': self.completion})
            elif attribute == PhantomActions.new_file.value:
                new_tab = (self.view.window() or active_window()).new_file(
                    flags=NewFileFlags.ADD_TO_SELECTION | NewFileFlags.CLEAR_TO_RIGHT,
                    syntax='Packages/Markdown/MultiMarkdown.sublime-syntax',
                )
                logger.debug(f'self.is_discardable: {self.is_discardable}')
                new_tab.set_scratch(self.is_discardable)
                new_tab.run_command('text_stream_at', {'position': 0, 'text': self.completion})
            elif attribute == PhantomActions.history.value:
                new_message = {
                    'role': 'assistant',
                    'content': self.completion,
                    'name': 'OpenAI_completion',
                }
                self.cacher.append_to_cache(self.user_input)
                self.cacher.append_to_cache([new_message])
                self.listner.update_output_view('\n\n## Question\n\n', self.view.window())  # type: ignore
                # MARK: \n\n for splitting command from selected text
                # FIXME: This logic adds redundant line breaks on a single message.
                [
                    self.listner.update_output_view(question['content'] + '\n\n', self.view.window())  # type: ignore
                    for question in self.user_input
                ]
                self.listner.update_output_view('\n\n## Answer\n\n', self.view.window())  # type: ignore
                self.listner.update_output_view(self.completion, self.view.window())  # type: ignore
            elif attribute == PhantomActions.close.value:
                pass

            self.phantom_set.update([])
            self.view.settings().set(VIEW_SETTINGS_KEY_OPENAI_TEXT, False)
        else:  # for handling all the rest URLs
            (self.view.window() or active_window()).run_command('open_url', {'url': attribute})


class PhantomActions(Enum):
    close = 'close'
    copy = 'copy'
    append = 'append'
    replace = 'replace'
    new_file = 'new_file'
    history = 'history'
