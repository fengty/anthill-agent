"""The shared default registry, pre-populated with built-in plugins."""

from __future__ import annotations

from anthill.plugins.base import PluginRegistry
from anthill.plugins.browser import BrowserRenderPlugin, BrowserScreenshotPlugin
from anthill.plugins.code_exec import CodeExecPlugin
from anthill.plugins.documents import DocxReadPlugin, PdfReadPlugin, XlsxReadPlugin
from anthill.plugins.filesystem import FileListPlugin, FileReadPlugin, FileWritePlugin
from anthill.plugins.shell import ShellPlugin
from anthill.plugins.web import WebFetchPlugin, WebSearchPlugin


def _build_default() -> PluginRegistry:
    reg = PluginRegistry()
    reg.register(WebFetchPlugin())
    reg.register(WebSearchPlugin())
    reg.register(FileReadPlugin())
    reg.register(FileWritePlugin())
    reg.register(FileListPlugin())
    reg.register(ShellPlugin())
    reg.register(CodeExecPlugin())
    reg.register(PdfReadPlugin())
    reg.register(DocxReadPlugin())
    reg.register(XlsxReadPlugin())
    reg.register(BrowserRenderPlugin())
    reg.register(BrowserScreenshotPlugin())
    return reg


default_registry = _build_default()
