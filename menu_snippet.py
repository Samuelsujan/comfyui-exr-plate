# Add these lines to your ~/.nuke/menu.py
import nuke_comfy_bridge
nuke.menu("Nuke").addCommand("Samuelsujan/Send Plate to Load EXR Plate",
    "nuke_comfy_bridge.send_plate()", "F9")
nuke.menu("Nuke").addCommand("Samuelsujan/Run Workflow on Selected (full bridge)",
    "nuke_comfy_bridge.run_bridge()", "F10")
