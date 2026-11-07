"""Verify wrapping against real Tk controls without displaying a visible window."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main


class LogWrappingTests(unittest.TestCase):
    def test_resize_reflows_readonly_log_without_changing_text(self):
        text = '\u8fd0\u884c\u65e5\u5fd7\uff1a' + 'LONG_PATH_OR_RECORD_ID_' * 40
        observed = {}

        def inspect_window():
            root = main.root
            # A transparent mapped window lets Tk measure actual display lines without a visible GUI.
            root.attributes('-alpha', 0.0)
            root.deiconify()
            root.geometry('580x740')
            root.update()
            widget = main.log_widget
            self.assertEqual(widget.cget('wrap'), 'word')
            self.assertEqual(widget.cget('state'), 'disabled')
            self.assertTrue(widget.bind('<Button-3>'))
            self.assertTrue(widget.bind('<Control-c>'))
            main._clear_log_widget()
            main._append(text)
            root.update_idletasks()
            narrow = widget.count('1.0', '1.end', 'displaylines')[0]
            root.geometry('900x740')
            root.update()
            wide = widget.count('1.0', '1.end', 'displaylines')[0]
            self.assertGreater(narrow, wide)
            self.assertGreater(wide, 0)
            self.assertEqual(widget.get('1.0', 'end-1c'), text + '\n')
            widget.insert('1.0', 'USER_EDIT_NOT_ALLOWED')
            self.assertEqual(widget.get('1.0', 'end-1c'), text + '\n')
            main._append_styled('RESULT', 'followup_success')
            self.assertEqual(widget.cget('state'), 'disabled')
            self.assertTrue(widget.get('1.0', 'end-1c').endswith('RESULT\n'))
            main._clear_log_widget()
            self.assertEqual(widget.get('1.0', 'end-1c'), '')
            self.assertEqual(widget.cget('state'), 'disabled')
            observed.update(narrow=narrow + 1, wide=wide + 1)
            root.withdraw()

        with patch('tkinter.Tk.mainloop', side_effect=inspect_window), \
             patch.object(main, 'log'), \
             patch.object(main, '_ensure_log_file', return_value='offline'), \
             patch.object(main, '_sync_saved_credentials_to_ui'), \
             patch.object(main, '_drain_log_queue'), \
             patch.object(main, '_poll_run_state'):
            try:
                main.run_gui(startup_check=True)
            finally:
                if main.root is not None:
                    try:
                        main.root.destroy()
                    except main.tk.TclError:
                        pass
        self.assertTrue(observed)
        print('LOG_WRAP_REAL_TK width580_lines=%d width900_lines=%d' % (observed['narrow'], observed['wide']))


if __name__ == '__main__':
    unittest.main()
