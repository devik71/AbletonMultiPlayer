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
    "rect": [140.0, 140.0, 860.0, 610.0],
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
          "patching_rect": [8.0, 8.0, 844.0, 572.0],
          "presentation": 1,
          "presentation_rect": [0.0, 0.0, 840.0, 570.0],
          "bgcolor": [0.098039, 0.109804, 0.105882, 1.0],
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
          "patching_rect": [24.0, 20.0, 320.0, 24.0],
          "presentation": 1,
          "presentation_rect": [18.0, 16.0, 330.0, 24.0],
          "fontname": "Arial",
          "fontsize": 16.0,
          "textcolor": [0.92, 0.96, 0.94, 1.0],
          "text": "AbletonMP Multiplayer Status"
        }
      },
      {
        "box": {
          "id": "obj-status",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [410.0, 18.0, 410.0, 22.0],
          "presentation": 1,
          "presentation_rect": [410.0, 16.0, 412.0, 22.0],
          "text": "offline",
          "bgcolor": [0.07451, 0.086275, 0.082353, 1.0],
          "textcolor": [0.85, 0.9, 0.87, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-url-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 62.0, 70.0, 20.0],
          "presentation": 1,
          "presentation_rect": [18.0, 54.0, 70.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Relay"
        }
      },
      {
        "box": {
          "id": "obj-url",
          "maxclass": "textedit",
          "numinlets": 1,
          "numoutlets": 4,
          "outlettype": ["", "int", "", ""],
          "patching_rect": [90.0, 58.0, 320.0, 28.0],
          "presentation": 1,
          "presentation_rect": [90.0, 52.0, 318.0, 28.0],
          "text": "http://127.0.0.1:19870",
          "bgcolor": [0.047059, 0.058824, 0.054902, 1.0],
          "textcolor": [0.93, 0.96, 0.94, 1.0],
          "bordercolor": [0.23, 0.28, 0.25, 1.0],
          "rounded": 4
        }
      },
      {
        "box": {
          "id": "obj-prepend-url",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [90.0, 94.0, 92.0, 22.0],
          "text": "prepend seturl"
        }
      },
      {
        "box": {
          "id": "obj-session-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [430.0, 62.0, 70.0, 20.0],
          "presentation": 1,
          "presentation_rect": [426.0, 54.0, 70.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Room"
        }
      },
      {
        "box": {
          "id": "obj-session",
          "maxclass": "textedit",
          "numinlets": 1,
          "numoutlets": 4,
          "outlettype": ["", "int", "", ""],
          "patching_rect": [492.0, 58.0, 160.0, 28.0],
          "presentation": 1,
          "presentation_rect": [486.0, 52.0, 158.0, 28.0],
          "text": "",
          "bgcolor": [0.047059, 0.058824, 0.054902, 1.0],
          "textcolor": [0.93, 0.96, 0.94, 1.0],
          "bordercolor": [0.23, 0.28, 0.25, 1.0],
          "rounded": 4
        }
      },
      {
        "box": {
          "id": "obj-prepend-session",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [492.0, 94.0, 102.0, 22.0],
          "text": "prepend session"
        }
      },
      {
        "box": {
          "id": "obj-refresh",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [672.0, 58.0, 70.0, 24.0],
          "presentation": 1,
          "presentation_rect": [664.0, 52.0, 74.0, 28.0],
          "text": "refresh",
          "bgcolor": [0.0, 0.454902, 0.345098, 1.0],
          "textcolor": [0.95, 1.0, 0.98, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-start",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [748.0, 58.0, 52.0, 24.0],
          "presentation": 1,
          "presentation_rect": [744.0, 52.0, 44.0, 28.0],
          "text": "start",
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
          "patching_rect": [804.0, 58.0, 44.0, 24.0],
          "presentation": 1,
          "presentation_rect": [792.0, 52.0, 30.0, 28.0],
          "text": "stop",
          "bgcolor": [0.32, 0.12, 0.11, 1.0],
          "textcolor": [1.0, 0.9, 0.88, 1.0]
        }
      },
      {
        "box": {
          "id": "obj-log-label",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 132.0, 150.0, 20.0],
          "presentation": 1,
          "presentation_rect": [18.0, 104.0, 150.0, 20.0],
          "textcolor": [0.68, 0.74, 0.71, 1.0],
          "text": "Relay Monitor"
        }
      },
      {
        "box": {
          "id": "obj-body",
          "maxclass": "textedit",
          "numinlets": 1,
          "numoutlets": 4,
          "outlettype": ["", "int", "", ""],
          "patching_rect": [24.0, 158.0, 806.0, 360.0],
          "presentation": 1,
          "presentation_rect": [18.0, 128.0, 804.0, 410.0],
          "text": "Waiting for relay...",
          "fontname": "Menlo",
          "fontsize": 11.0,
          "bgcolor": [0.047059, 0.058824, 0.054902, 1.0],
          "textcolor": [0.88, 0.93, 0.9, 1.0],
          "bordercolor": [0.23, 0.28, 0.25, 1.0],
          "rounded": 4
        }
      },
      {
        "box": {
          "id": "obj-note",
          "maxclass": "comment",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [24.0, 532.0, 800.0, 22.0],
          "presentation": 1,
          "presentation_rect": [18.0, 542.0, 804.0, 18.0],
          "fontsize": 10.0,
          "textcolor": [0.52, 0.58, 0.55, 1.0],
          "text": "Reads relay /health. Shows room status, online players, IPs, Live/script versions, action counts, last event and event-type breakdown."
        }
      },
      {
        "box": {
          "id": "obj-js",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 3,
          "outlettype": ["", "", ""],
          "patching_rect": [248.0, 94.0, 208.0, 22.0],
          "text": "js abletonmp_multiplayer_status.js"
        }
      },
      {
        "box": {
          "id": "obj-print",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 0,
          "patching_rect": [470.0, 132.0, 162.0, 22.0],
          "text": "print AbletonMP-Status"
        }
      },
      {
        "box": {
          "id": "obj-loadbang",
          "maxclass": "newobj",
          "numinlets": 1,
          "numoutlets": 1,
          "outlettype": ["bang"],
          "patching_rect": [650.0, 94.0, 58.0, 22.0],
          "text": "loadbang"
        }
      },
      {
        "box": {
          "id": "obj-load-start",
          "maxclass": "message",
          "numinlets": 2,
          "numoutlets": 1,
          "outlettype": [""],
          "patching_rect": [718.0, 94.0, 42.0, 22.0],
          "text": "start"
        }
      }
    ],
    "lines": [
      { "patchline": { "source": ["obj-url", 0], "destination": ["obj-prepend-url", 0] } },
      { "patchline": { "source": ["obj-prepend-url", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-session", 0], "destination": ["obj-prepend-session", 0] } },
      { "patchline": { "source": ["obj-prepend-session", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-refresh", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-start", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-stop", 0], "destination": ["obj-js", 0] } },
      { "patchline": { "source": ["obj-js", 0], "destination": ["obj-status", 1] } },
      { "patchline": { "source": ["obj-js", 1], "destination": ["obj-body", 0] } },
      { "patchline": { "source": ["obj-js", 2], "destination": ["obj-print", 0] } },
      { "patchline": { "source": ["obj-loadbang", 0], "destination": ["obj-load-start", 0] } },
      { "patchline": { "source": ["obj-load-start", 0], "destination": ["obj-js", 0] } }
    ],
    "dependency_cache": [
      {
        "name": "abletonmp_multiplayer_status.js",
        "bootpath": "./",
        "patcherrelativepath": ".",
        "type": "TEXT",
        "implicit": 1
      }
    ],
    "autosave": 0
  }
}
