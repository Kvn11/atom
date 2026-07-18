"""Built-in workflows bundled with the atom package (shipped as package-data).

These are core-platform workflows (e.g. self-improve) that must be available on every
install without the user copying anything into \$ATOM_HOME/workflows/. Resolution falls back
here when a workflow is not found in the user's \$ATOM_HOME/workflows/ dir; a user file of the
same name overrides the built-in. Located at runtime via __file__ (see workflow.schema).
"""
