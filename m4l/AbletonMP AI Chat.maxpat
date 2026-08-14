{
  "patcher": {
    "fileversion": 1,
    "appversion": {
      "major": 8,
      "minor": 6,
      "revision": 0,
      "architecture": "x64",
      "modernui": 1
    },
    "classnamespace": "box",
    "rect": [120.0, 120.0, 780.0, 560.0],
    "bglocked": 0,
    "openinpresentation": 1,
    "default_fontsize": 12.0,
    "default_fontface": 0,
    "default_fontname": "Arial",
    "gridonopen": 1,
    "gridsize": [15.0, 15.0],
    "objectsnaponopen": 1,
    "statusbarvisible": 2,
    "toolbarvisible": 1,
    "lefttoolbarpinned": 0,
    "toptoolbarpinned": 0,
    "righttoolbarpinned": 0,
    "bottomtoolbarpinned": 0,
    "toolbars_unpinned_last_save": 0,
    "tallnewobj": 0,
    "boxanimatetime": 200,
    "enablehscroll": 1,
    "enablevscroll": 1,
    "boxes": [
      {
        "box": {
          "id": "obj-bg",
          "maxclass": "panel",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [8.0, 8.0, 764.0, 528.0],
          "presentation": 1,
          "presentation_rect": [0.0, 0.0, 760.0, 520.0],
          "bgcolor": [0.105882, 0.121569, 0.113725, 1.0],
          "bordercolor": [0.2, 0.235294, 0.219608, 1.0],
          "rounded": 0
        }
      },
      {
        "box": {
          "id": "obj-title",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 20.0, 260.0, 24.0],
          "presentation": 1,
          "presentation_rect": [18.0, 16.0, 270.0, 24.0],
          "fontname": "Arial",
          "fontsize": 16.0,
          "textcolor": [0.92, 0.96, 0.94, 1.0],
          "text": "AbletonMP AI Chat"
        }
      },
      {
        "box": {
          "id": "obj-status-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [470.0, 20.0, 58.0, 20.0],
          "presentation": 1,
          "presentation_rect": [486.0, 18.0, 52.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Status"
        }
      },
      {
        "box": {
          "id": "obj-status",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [530.0, 18.0, 210.0, 22.0],
          "presentation": 1,
          "presentation_rect": [540.0, 16.0, 202.0, 22.0],
          "text": "offline",
          "bgcolor": [0.07451, 0.086275, 0.082353, 1.0],
          "textcolor": [0.85, 0.9, 0.87, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-prompt-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 58.0, 120.0, 20.0],
          "presentation": 1,
          "presentation_rect": [18.0, 52.0, 120.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Prompt"
        }
      },
      {
        "box": {
          "id": "obj-prompt",
          "maxclass": "textedit",
          "numinlets": 1,
          "numoutlets": 4,
          "outlettype": ["", "int", "", ""],
          "patching_rect": [24.0, 82.0, 520.0, 110.0],
          "presentation": 1,
          "presentation_rect": [18.0, 76.0, 520.0, 110.0],
          "text": "Create a new scene called M4L Demo and make a short MIDI bass clip.",
          "bgcolor": [0.047059, 0.058824, 0.054902, 1.0],
          "textcolor": [0.93, 0.96, 0.94, 1.0],
          "bordercolor": [0.23, 0.28, 0.25, 1.0],
          "rounded": 4
        }
      },
      {
        "box": {
          "id": "obj-prepend-prompt",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [24.0, 202.0, 110.0, 22.0],
          "text": "prepend setprompt"
        }
      },
      {
        "box": {
          "id": "obj-token-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 214.0, 120.0, 20.0],
          "presentation": 1,
          "presentation_rect": [18.0, 208.0, 120.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Token"
        }
      },
      {
        "box": {
          "id": "obj-token",
          "maxclass": "textedit",
          "numinlets": 1,
          "numoutlets": 4,
          "outlettype": ["", "int", "", ""],
          "patching_rect": [24.0, 238.0, 520.0, 28.0],
          "presentation": 1,
          "presentation_rect": [18.0, 232.0, 520.0, 28.0],
          "text": "",
          "bgcolor": [0.047059, 0.058824, 0.054902, 1.0],
          "textcolor": [0.93, 0.96, 0.94, 1.0],
          "bordercolor": [0.23, 0.28, 0.25, 1.0],
          "rounded": 4
        }
      },
      {
        "box": {
          "id": "obj-prepend-token",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [24.0, 274.0, 92.0, 22.0],
          "text": "prepend token"
        }
      },
      {
        "box": {
          "id": "obj-execute-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [566.0, 78.0, 88.0, 20.0],
          "presentation": 1,
          "presentation_rect": [560.0, 76.0, 88.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Execute"
        }
      },
      {
        "box": {
          "id": "obj-execute-toggle",
          "maxclass": "toggle",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": ["int"],
          "patching_rect": [654.0, 76.0, 24.0, 24.0],
          "presentation": 1,
          "presentation_rect": [650.0, 74.0, 24.0, 24.0],
          "checkedcolor": [0.0, 0.815686, 0.698039, 1.0],
          "uncheckedcolor": [0.18, 0.21, 0.2, 1.0],
          "parameter_enable": 0
        }
      },
      {
        "box": {
          "id": "obj-load-execute",
          "maxclass": "loadmess",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [654.0, 46.0, 70.0, 22.0],
          "text": "1"
        }
      },
      {
        "box": {
          "id": "obj-prepend-execute",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [654.0, 108.0, 104.0, 22.0],
          "text": "prepend execute"
        }
      },
      {
        "box": {
          "id": "obj-ask",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [566.0, 116.0, 72.0, 24.0],
          "presentation": 1,
          "presentation_rect": [560.0, 116.0, 82.0, 24.0],
          "text": "ask",
          "bgcolor": [0.0, 0.454902, 0.345098, 1.0],
          "textcolor": [0.95, 1.0, 0.98, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-snapshot",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [648.0, 116.0, 78.0, 24.0],
          "presentation": 1,
          "presentation_rect": [650.0, 116.0, 92.0, 24.0],
          "text": "snapshot",
          "bgcolor": [0.12549, 0.156863, 0.141176, 1.0],
          "textcolor": [0.9, 0.95, 0.92, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-stop",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [566.0, 150.0, 72.0, 24.0],
          "presentation": 1,
          "presentation_rect": [560.0, 150.0, 82.0, 24.0],
          "text": "stop",
          "bgcolor": [0.32, 0.12, 0.11, 1.0],
          "textcolor": [1.0, 0.9, 0.88, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-status-button",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [648.0, 150.0, 78.0, 24.0],
          "presentation": 1,
          "presentation_rect": [650.0, 150.0, 92.0, 24.0],
          "text": "status",
          "bgcolor": [0.12549, 0.156863, 0.141176, 1.0],
          "textcolor": [0.9, 0.95, 0.92, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-runjson",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [566.0, 184.0, 160.0, 24.0],
          "presentation": 1,
          "presentation_rect": [560.0, 184.0, 182.0, 24.0],
          "text": "runjson",
          "bgcolor": [0.12549, 0.156863, 0.141176, 1.0],
          "textcolor": [0.9, 0.95, 0.92, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-response-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 312.0, 120.0, 20.0],
          "presentation": 1,
          "presentation_rect": [18.0, 284.0, 120.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Response"
        }
      },
      {
        "box": {
          "id": "obj-response",
          "maxclass": "textedit",
          "numinlets": 1,
          "numoutlets": 4,
          "outlettype": ["", "int", "", ""],
          "patching_rect": [24.0, 336.0, 700.0, 160.0],
          "presentation": 1,
          "presentation_rect": [18.0, 308.0, 724.0, 190.0],
          "text": "Click status. Then ask for a Live edit.",
          "bgcolor": [0.047059, 0.058824, 0.054902, 1.0],
          "textcolor": [0.88, 0.93, 0.9, 1.0],
          "bordercolor": [0.23, 0.28, 0.25, 1.0],
          "rounded": 4
        }
      },
      {
        "box": {
          "id": "obj-js",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 3,
          "outlettype": ["", "", ""],
          "patching_rect": [238.0, 274.0, 152.0, 22.0],
          "text": "js abletonmp_chat.js"
        }
      },
      {
        "box": {
          "id": "obj-print",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [412.0, 308.0, 124.0, 22.0],
          "text": "print AbletonMP-AI"
        }
      },
      {
        "box": {
          "id": "obj-loadbang",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": ["bang"],
          "patching_rect": [430.0, 238.0, 58.0, 22.0],
          "text": "loadbang"
        }
      },
      {
        "box": {
          "id": "obj-load-status",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [500.0, 238.0, 48.0, 22.0],
          "text": "status"
        }
      },
      {
        "box": {
          "id": "obj-note",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 506.0, 700.0, 20.0],
          "presentation": 1,
          "presentation_rect": [18.0, 500.0, 724.0, 18.0],
          "fontsize": 10.0,
          "textcolor": [0.52, 0.58, 0.55, 1.0],
          "text": "Backend: AbletonMP Remote Script on localhost. Execute off = plan preview; runjson = execute JSON typed in Prompt."
        }
      }
    ],
    "lines": [
      { "patchline": { "source": ["obj-prompt", 0], "destination": ["obj-prepend-prompt", 0] } },
      { "patchline": { "source": ["obj-prepend-prompt", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-token", 0], "destination": ["obj-prepend-token", 0] } },
      { "patchline": { "source": ["obj-prepend-token", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-load-execute", 0], "destination": ["obj-execute-toggle", 0] } },
      { "patchline": { "source": ["obj-execute-toggle", 0], "destination": ["obj-prepend-execute", 0] } },
      { "patchline": { "source": ["obj-prepend-execute", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-ask", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-snapshot", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-stop", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-status-button", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-runjson", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-js", 0], "destination": ["obj-status", 1] } },
      { "patchline": { "source": ["obj-js", 1], "destination": ["obj-response", 0] } },
      { "patchline": { "source": ["obj-js", 2], "destination": ["obj-print", 0] } },
      { "patchline": { "source": ["obj-loadbang", 0], "destination": ["obj-load-status", 0] } },
      { "patchline": { "source": ["obj-load-status", 0], "destination": ["obj-js", 0] } }
    ],
    "dependency_cache": [
      {
        "name": "abletonmp_chat.js",
        "bootpath": "./",
        "patcherrelativepath": ".",
        "type": "TEXT",
        "implicit": 1
      }
    ],
    "autosave": 0
  }
}
