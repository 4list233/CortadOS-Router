"""CortadOS Router: picks a model for every LLM call from typed request scores, routing tables and live
subscription scarcity. See README.md and docs/ROUTER-DESIGN.md.

Loading convention (unchanged from CortadOS): core, reviews, images and transport are loaded BY FILE PATH under
unique module names (the Hermes plugin and the tests do this, because other Hermes plugins ship clashing names
such as `core`), and core loads scarcity → probe_reaper / unitcost, cron_routing loads execution_routing, the same
way. compat, paths, task_context, execution_routing and cron_routing are imported as package modules
(`cortados_router.<name>`); every path-loaded file first puts this repository's root on sys.path for that.
Importing this package does nothing on its own.
"""
