# FreeCAD runs this when it starts. This folder (the CADSwarm add-on) is on sys.path,
# so the `cadswarm` package beside this file is importable.
import FreeCADGui as Gui
from PySide import QtCore


def open_on_start():
    """Open the team panel on every launch, not only when someone picks the CADSwarm workbench.

    Starting FreeCAD plainly (its Start page, or a model opened from Explorer) never runs the
    workbench's Activated(), so the panel — and with it the join — only came up if you found
    CADSwarm in the workbench list. That is what "sometimes it doesn't connect" looked like. The
    panel rejoins the team by itself (panel.try_auto_rejoin), so this is the whole of "start CADSwarm".
    """
    try:
        from cadswarm.gui import panel
        if panel.SESSION.panel is None:
            panel.show_panel()
        panel.SESSION.try_auto_rejoin()
    except Exception as e:  # noqa: BLE001 - never break FreeCAD's startup over this
        import FreeCAD
        FreeCAD.Console.PrintWarning(f"[CADSwarm] could not open the team panel: {e}\n")


QtCore.QTimer.singleShot(2000, open_on_start)


class CadSwarmWorkbench(Gui.Workbench):
    MenuText = "CADSwarm"
    ToolTip = "Build one 3D model together with your team's AI helpers"
    Icon = """
/* XPM */
static char * icon_xpm[] = {
"16 16 3 1", " 	c None", ".	c #2E86DE", "+	c #F39C12",
"                ", "   ....  ++++   ", "  ...... ++++++ ", "  ...... ++++++ ",
"  ...... ++++++ ", "   ....  ++++   ", "                ", "      ......    ",
"     ........   ", "     ........   ", "     ........   ", "      ......    ",
"                ", "                ", "                ", "                "};
"""

    def Initialize(self):
        from cadswarm.gui import panel
        panel.register_commands()
        from cadswarm.gui import overlay
        overlay.register_command()
        overlay.install()
        from cadswarm.gui.structural import register_command
        register_command()
        from cadswarm.gui import lighten_ui
        lighten_ui.register_command()
        # Everything else lives in the team panel's tabs; the toolbar only opens it (and the setup guide).
        self.appendToolbar("CADSwarm", ["CadSwarm_Connect", "CadSwarm_Panel"])
        self.appendMenu("CADSwarm", ["CadSwarm_Connect", "CadSwarm_Panel", "CadSwarm_NewTask", "CadSwarm_TellHelper",
                                     "CadSwarm_EditPart", "CadSwarm_Library", "CadSwarm_Settings", "CadSwarm_Style",
                                     "CadSwarm_Constrain", "CadSwarm_AddHelper", "CadSwarm_Invite", "Separator", "CadSwarm_Interference",
                                     "CadSwarm_Export", "CadSwarm_Lighten", "CadSwarm_OptimizePart", "CadSwarm_RunFea",
                                     "CadSwarm_FeaReports",
                                     "CadSwarm_Pointers", "Separator", "CadSwarm_Lock", "CadSwarm_Release",
                                     "Separator", "CadSwarm_Disconnect", "CadSwarm_StopHosting"])

    def Activated(self):
        from cadswarm.gui import panel
        panel.show_panel()
        panel.SESSION.try_auto_rejoin()  # first time: opens the setup guide; later: back in your team

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(CadSwarmWorkbench())


class CadSwarmOptimizationWorkbench(Gui.Workbench):
    """Local structural workflow, available without starting a collaboration session."""
    MenuText = "CADSwarm Optimization"
    ToolTip = "Analyze and optimize a solid locally with CalculiX"
    Icon = ""

    def Initialize(self):
        from cadswarm.gui.structural import register_command
        register_command()
        from cadswarm.gui import lighten_ui
        lighten_ui.register_command()
        commands = ["CadSwarm_OptimizationDemo", "CadSwarm_Lighten", "CadSwarm_OptimizePart"]
        self.appendToolbar("Structural optimization", commands)
        self.appendMenu("Optimization", commands)

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(CadSwarmOptimizationWorkbench())
