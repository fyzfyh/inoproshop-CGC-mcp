# encoding: utf-8
"""Private owned-host modal fixture, loaded only by the acceptance wrapper."""
import io
import json
import os
import time
import clr
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
from System.Windows.Forms import Application, Form, FormStartPosition, Timer
from System.Drawing import Point, Size
from System.Threading import Thread

_modal_probe_root = INOPROSHOP_MODAL_PROBE_ROOT
_modal_probe_timer = Timer()
_modal_probe_timer.Interval = 100
_modal_probe_done = False


def _modal_marker(name, value):
    destination = os.path.join(_modal_probe_root, name + '.json')
    with io.open(destination + '.tmp', 'w', encoding='utf-8') as handle:
        handle.write(unicode(json.dumps(value)))
    os.rename(destination + '.tmp', destination)


def _modal_probe_tick(sender, event):
    global _modal_probe_done
    if _modal_probe_done or not os.path.exists(os.path.join(_modal_probe_root, 'modal_trigger')):
        return
    _modal_probe_done = True
    _modal_probe_timer.Stop()
    dialog = Form()
    dialog.Text = 'Owned MCP acceptance modal'
    dialog.ShowInTaskbar = False
    dialog.StartPosition = FormStartPosition.Manual
    dialog.Location = Point(-32000, -32000)
    dialog.Size = Size(200, 100)
    dialog.Opacity = 0.0
    closer = Timer()
    closer.Interval = 8000
    def close_modal(sender, event):
        closer.Stop()
        dialog.Close()
    def shown(sender, event):
        _modal_marker('modal_opened', {'at': time.time(), 'modal': bool(dialog.Modal),
                      'thread_id': Thread.CurrentThread.ManagedThreadId})
        closer.Start()
    closer.Tick += close_modal
    dialog.Shown += shown
    try:
        owner = next((form for form in Application.OpenForms if form is not dialog and not form.Modal), None)
        if owner is not None: dialog.ShowDialog(owner)
        else: dialog.ShowDialog()
        _modal_marker('modal_closed', {'at': time.time(), 'automatically_closed': True})
    finally:
        closer.Dispose()
        dialog.Dispose()


_modal_probe_timer.Tick += _modal_probe_tick
_modal_probe_timer.Start()
