"""Профиль TypeScript/JavaScript (+ TSX/JSX) — плагин Vision03."""

from ...handlers.typescript_handler import TS_SPEC, TSX_SPEC
from .base import TSProfile

# namespace/module оборачиваются в internal_module/module — прозрачные рамки.
_FRAMES = {'internal_module', 'module'}
# NAME = () => {} / NAME = function(){} — привязка функции к имени: промоушен в landmark.
_BINDERS = ('variable_declarator', 'field_definition', 'public_field_definition',
            'pair', 'assignment_expression')
_VALUES = ('arrow_function', 'function', 'function_expression')

TS = TSProfile(TS_SPEC, extra_frames=_FRAMES, binders=_BINDERS, value_types=_VALUES)
TSX = TSProfile(TSX_SPEC, extra_frames=_FRAMES, binders=_BINDERS, value_types=_VALUES)
