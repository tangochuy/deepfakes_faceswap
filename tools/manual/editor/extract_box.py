#!/usr/bin/env python3
""" Extract Box Editor for the manual adjustments tool """

import platform

import numpy as np

from lib.gui.custom_widgets import RightClickMenu
from ._base import Editor, logger


class ExtractBox(Editor):
    """ The Extract Box Editor. """
    def __init__(self, canvas, detected_faces, frames):
        self._right_click_menu = RightClickMenu(["Delete Face"],
                                                [self._delete_current_face],
                                                ["Del"])
        control_text = ("Extract Box Editor\nMove the extract box that has been generated by the "
                        "aligner.\n\n"
                        " - Click and drag the bounding box to relocate the landmarks without "
                        "recalculating them.")
        key_bindings = {"<Delete>": self._delete_current_face}
        super().__init__(canvas, detected_faces, frames,
                         control_text=control_text, key_bindings=key_bindings)

    def update_annotation(self):
        """ Draw the Extract Box around faces and set the object to :attr:`_object`"""
        keys = ("text", "extractbox")
        color = self._control_color
        for idx, face in enumerate(self._det_faces.current_faces[self._frame_index]):
            logger.trace("Drawing Extract Box: (idx: %s, roi: %s)", idx, face.original_roi)
            if self._is_zoomed:
                box = np.array((self._zoomed_roi[0], self._zoomed_roi[1],
                                self._zoomed_roi[2], self._zoomed_roi[1],
                                self._zoomed_roi[2], self._zoomed_roi[3],
                                self._zoomed_roi[0], self._zoomed_roi[3]))
            else:
                face.load_aligned(None)
                box = self._scale_to_display(face.original_roi).flatten()
            top_left = box[:2] - 10
            kwargs = dict(fill=color, font=("Default", 20, "bold"), text=str(idx))
            self._object_tracker(keys[0], "text", idx, 0, top_left, kwargs)
            kwargs = dict(fill="", outline=color, width=1)
            self._object_tracker(keys[1], "polygon", idx, 0, box, kwargs)
            # Ensure extract box is above other annotations for mouse grabber
            self._canvas.tag_raise(self._objects[keys[1]][idx][0])
        logger.trace("Updated extract box annotations: %s", {key: self._objects.get(key, None)
                                                             for key in keys})

    # << MOUSE HANDLING >>
    # Mouse cursor display
    def _update_cursor(self, event):
        """ Update the cursors for hovering over extract boxes and update
        :attr:`_mouse_location`. """
        extract_boxes = self._flatten_list(self._objects.get("extractbox", []))
        item_ids = set(self._canvas.find_withtag("current")).intersection(extract_boxes)
        if not item_ids:
            self._canvas.config(cursor="")
            self._mouse_location = None
        else:
            item_id = list(item_ids)[0]
            self._canvas.config(cursor="fleur")
            self._mouse_location = [face_idx
                                    for face_idx, face in enumerate(self._objects["extractbox"])
                                    if item_id in face][0]

    # Mouse click actions
    def set_mouse_click_actions(self):
        """ Add right click context menu to default mouse click bindings """
        super().set_mouse_click_actions()
        self._canvas.bind("<Button-2>" if platform.system() == "Darwin" else "<Button-3>",
                          self._context_menu)

    def _drag_start(self, event):
        """ The action to perform when the user starts clicking and dragging the mouse.

        Collect information about the object being clicked on and add to :attr:`_drag_data`

        Parameters
        ----------
        event: :class:`tkinter.Event`
            The tkinter mouse event.
        """
        if self._mouse_location is None:
            self._drag_data = dict()
            return
        self._drag_data["current_location"] = (event.x, event.y)
        self._drag_callback = self._move

    def _move(self, event):
        """ Moves the Extract box and the underlying landmarks on an extract box drag event.

        Parameters
        ----------
        event: :class:`tkinter.Event`
            The tkinter mouse event.

        """
        if not self._drag_data:
            return
        shift_x = event.x - self._drag_data["current_location"][0]
        shift_y = event.y - self._drag_data["current_location"][1]
        for obj in self._objects.values():
            self._canvas.move(obj[self._mouse_location][0], shift_x, shift_y)
        scaled_shift = self.scale_from_display(np.array((shift_x, shift_y)), do_offset=False)
        self._det_faces.update.landmarks(self._frame_index, self._mouse_location, *scaled_shift)
        self._drag_data["current_location"] = (event.x, event.y)

    def _context_menu(self, event):
        """ Create a right click context menu to delete the alignment that is being
        hovered over. """
        if self._mouse_location is None:
            return
        self._right_click_menu.popup(event)

    def _delete_current_face(self, *args):  # pylint:disable=unused-argument
        """ Called by the right click delete event. Deletes the face that the mouse is currently
        over.

        Parameters
        ----------
        args: tuple (unused)
            The event parameter is passed in by the hot key binding, so args is required
        """
        if self._mouse_location is None:
            return
        self._det_faces.update.delete(self._frame_index, self._mouse_location)
