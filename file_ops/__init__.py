"""file_ops — feature group: view_file, create_file, modify_file, sift_files.

Marks this directory as an explicit subpackage of the hermes-filetools plugin
(vs. an implicit namespace package) so Hermes' loader — which imports the
plugin root via `spec_from_file_location(..., submodule_search_locations=[...])`
— resolves `from .file_ops import view_file_core` reliably.
"""
