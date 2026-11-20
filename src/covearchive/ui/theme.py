"""System-following widget colors, semantic roles and explicit theme lifecycle."""
from PySide6.QtCore import QObject, Qt, Signal
from functools import lru_cache
from PySide6.QtGui import QColor, QPalette, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


TOKENS = {
    'light': dict(surface='#f5f1e8', card='#fffaf1', text='#29251e',
        toolbar='#eee8dd', center='#fffaf1', panel='#f0ebe1', input='#fffaf1', hover='#e9e2d6',
        line='#d8d0c3', faint='#655e52', muted='#655e52', border='#817668', accent='#006b5b', accent_text='#ffffff',
        danger='#a02f25', warning='#805500', success='#17643c', selection='#d4e8de',
        selection_text='#213b32', focus='#006b5b', disabled='#655e52', disabled_surface='#e9e2d6'),
    'dark': dict(surface='#1c1b19', card='#181715', text='#ece6d6',
        toolbar='#161513', center='#141312', panel='#181715', input='#0f0e0d', hover='#24221f',
        line='#2e2b27', faint='#a8a194', muted='#a8a194', border='#897f70', accent='#5fd4c0', accent_text='#0b2622',
        danger='#ef8b7c', warning='#e3bd6e', success='#93d4a6', selection='#1b2624',
        selection_text='#ece6d6', focus='#5fd4c0', disabled='#a8a194', disabled_surface='#24221f'),
}


@lru_cache(maxsize=1)
def _fixed_family():
    families=QFontDatabase.families()
    return next((name for name in ('Cascadia Mono','Consolas','DejaVu Sans Mono','Noto Sans Mono','Liberation Mono') if name in families),QFontDatabase.systemFont(QFontDatabase.FixedFont).family())


def fixed_font(widget):
    font=QFont(_fixed_family()); font.setPointSizeF(widget.font().pointSizeF()); return font


def set_property(widget, name, value):
    """Refresh dynamic-property selectors immediately, including hidden widgets."""
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_tone(widget, tone='normal'):
    set_property(widget, 'tone', tone)


def set_role(widget, role='secondary'):
    set_property(widget, 'role', role)


def resolve_scheme(scheme, palette):
    if scheme == Qt.ColorScheme.Dark:
        return 'dark'
    if scheme == Qt.ColorScheme.Light:
        return 'light'
    return 'dark' if palette.color(QPalette.Window).lightnessF() < .5 else 'light'


def current_tokens(widget=None):
    name = widget.window().property('themeName') if widget is not None else None
    if name not in TOKENS:
        app = QApplication.instance()
        name = resolve_scheme(app.styleHints().colorScheme(), app.palette())
    return TOKENS[name]


def build_palette(tokens):
    palette = QPalette()
    for role, key in ((QPalette.Window, 'surface'), (QPalette.WindowText, 'text'),
            (QPalette.Base, 'card'), (QPalette.AlternateBase, 'surface'),
            (QPalette.Text, 'text'), (QPalette.Button, 'surface'),
            (QPalette.ButtonText, 'text'), (QPalette.PlaceholderText, 'muted'),
            (QPalette.Highlight, 'selection'), (QPalette.HighlightedText, 'selection_text'),
            (QPalette.ToolTipBase, 'card'), (QPalette.ToolTipText, 'text'),
            (QPalette.Link, 'accent')):
        palette.setColor(role, QColor(tokens[key]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.PlaceholderText):
        palette.setColor(QPalette.Disabled, role, QColor(tokens['disabled']))
    return palette


def build_stylesheet(t):
    """One inherited sheet; system fonts and DPI remain Qt's responsibility."""
    return f'''
        QWidget {{ background: {t['surface']}; color: {t['text']}; }}
        QLabel {{ background: transparent; }}
        QWidget[stateSurface="true"] {{ background: {t['card']}; }}
        QLabel[chip="true"] {{ background: {t['hover']}; border: 1px solid {t['line']}; border-radius: 4px; padding: 2px; }}
        QLabel[inspectorHeading="true"] {{ color: {t['muted']}; font-weight: 600; }}
        QLabel[title="true"] {{ font-weight: 600; }}
        QLabel[stateHeading="true"] {{ font-size: 15px; font-weight: 600; color: {t['text']}; }}
        QLabel[stateHint="true"] {{ font-size: 11px; color: {t['muted']}; }}
        QFrame[examplePanel="true"] {{ background: {t['panel']}; border: 1px solid {t['line']}; border-radius: 6px; }}
        QProgressBar#scanBusy {{ min-height: 4px; max-height: 4px; border: none; }}
        QProgressBar#scanBusy::chunk {{ background: {t['accent']}; border: none; }}
        QStatusBar {{ border: none; padding: 2px 4px; }}
        QStatusBar::item {{ border: none; }}
        QMenu {{ background: {t['card']}; color: {t['text']}; border: 1px solid {t['border']}; }}
        QMenu::item:selected {{ background: {t['selection']}; color: {t['selection_text']}; }}
        QSplitter::handle {{ background: {t['line']}; margin-left: 2px; margin-right: 2px; }}
        QFrame#topToolbar {{ background: {t['toolbar']}; border-bottom: 1px solid {t['line']}; }}
        QFrame#urlContainer {{ background: {t['input']}; border: 1px solid {t['border']}; border-radius: 5px; }}
        QPushButton[chrome="true"] {{ background: transparent; border-color: transparent; padding: 4px; }}
        QPushButton[chrome="true"]:hover {{ background: {t['hover']}; border-color: {t['border']}; }}
        QPushButton[chrome="true"]:focus {{ border: 2px solid {t['focus']}; padding: 3px; }}
        QFrame[section="card"] {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 5px; }}
        QFrame[section="card"] QLabel, QFrame[section="card"] QCheckBox {{ background: transparent; }}
        QLabel[notice="true"] {{ background: {t['card']}; border-left: 3px solid {t['warning']}; padding: 5px 8px; }}
        QLabel[notice="true"][tone="error"] {{ border-left-color: {t['danger']}; }}
        QWidget#queuePage QPushButton {{ padding: 5px 11px; }}
        QWidget#queuePage QPushButton:focus {{ padding: 4px 10px; }}
        QWidget#queuePage QTableView::item {{ padding: 6px 4px; }}
        QLabel[heading="true"] {{ font-weight: 600; color: {t['muted']}; }}
        QWidget#libraryPage QPushButton {{ padding: 4px 8px; }}
        QWidget#libraryPage QPushButton:focus {{ padding: 3px 7px; }}
        QTableView#repositoryTable::item:selected {{ background: {t['selection']}; color: {t['selection_text']}; border-bottom: 1px solid {t['accent']}; }}
        QWidget#browsePage QPushButton {{ padding: 3px 8px; }}
        QWidget#browsePage QPushButton:focus {{ padding: 2px 7px; }}
        QWidget#browsePage QCheckBox {{ padding: 1px; spacing: 3px; border-radius: 4px; }}
        QWidget#browsePage QCheckBox:focus {{ padding: 0; }}
        QWidget#browsePage QCheckBox:checked {{ background: {t['selection']}; color: {t['selection_text']}; }}
        QLabel#providerBadge[tone="muted"] {{ background: {t['surface']}; color: {t['muted']}; }}
        QWidget#libraryPage QHeaderView::section {{ padding: 5px 2px; }}
        QTableView#repositoryTable {{ padding: 2px; }}
        QTableView#repositoryTable:focus {{ padding: 1px; }}
        QLabel#providerBadge {{ background: {t['selection']}; color: {t['selection_text']};
            border: 1px solid {t['border']}; border-radius: 8px; padding: 2px 8px; }}
        QLineEdit, QTextEdit, QPlainTextEdit, QAbstractItemView, QComboBox, QAbstractSpinBox {{
            background: {t['card']}; color: {t['text']};
            border: 1px solid {t['border']}; border-radius: 4px; padding: 4px;
            placeholder-text-color: {t['muted']};
            selection-background-color: {t['selection']}; selection-color: {t['selection_text']};
        }}
        QPushButton {{ background: {t['card']}; border: 1px solid {t['border']};
            border-radius: 4px; padding: 6px 11px; }}
        QPushButton:hover {{ border-color: {t['accent']}; }}
        QPushButton:pressed {{ background: {t['selection']}; color: {t['selection_text']}; }}
        QPushButton[role="primary"] {{ background: {t['accent']}; color: {t['accent_text']};
            border-color: {t['accent']}; font-weight: 400; }}
        QPushButton[role="danger"] {{ color: {t['danger']}; }}
        QWidget:disabled {{ color: {t['disabled']}; }}
        QPushButton:disabled, QLineEdit:disabled {{ background: {t['disabled_surface']};
            color: {t['disabled']}; border-color: {t['border']}; font-weight: 400;
            placeholder-text-color: {t['disabled']}; }}
        QCheckBox {{ spacing: 5px; border: 1px solid transparent; padding: 2px; }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QPushButton:focus,
        QCheckBox:focus, QAbstractItemView:focus, QComboBox:focus, QAbstractSpinBox:focus,
        QTabBar::tab:focus {{ border: 2px solid {t['focus']}; }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QAbstractItemView:focus,
        QComboBox:focus, QAbstractSpinBox:focus {{ padding: 3px; }}
        QPushButton:focus {{ padding: 5px 10px; }}
        QCheckBox:focus {{ padding: 1px; }}
        QPushButton[role="primary"]:focus {{ border: 2px solid {t['accent_text']}; }}
        QAbstractItemView::item:selected {{ background: {t['selection']}; color: {t['selection_text']}; }}
        QHeaderView, QHeaderView:focus {{ border: none; border-radius: 0; padding: 0; }}
        QHeaderView::section {{ background: {t['surface']}; color: {t['muted']};
            border: none; border-bottom: 1px solid {t['border']}; padding: 5px; }}
        QTableView {{ gridline-color: {t['border']}; }}
        QScrollBar {{ background: {t['surface']}; border: none; margin: 0; }}
        QScrollBar:vertical {{ width: 14px; }}
        QScrollBar:horizontal {{ height: 14px; }}
        QScrollBar::handle {{ background: {t['border']}; border-radius: 5px; }}
        QScrollBar::handle:vertical {{ min-height: 24px; margin: 2px; }}
        QScrollBar::handle:horizontal {{ min-width: 24px; margin: 2px; }}
        QScrollBar::handle:hover, QScrollBar::handle:pressed {{ background: {t['accent']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QTabWidget::pane {{ border: none; }}
        QTabBar::tab {{ background: {t['surface']}; color: {t['muted']};
            border: 1px solid transparent; padding: 7px 17px; }}
        QTabBar::tab:selected {{ background: {t['card']}; color: {t['text']};
            border-bottom: 2px solid {t['accent']}; padding-bottom: 6px; font-weight: 400; }}
        QTabBar::tab:focus {{ padding: 6px 16px; }}
        QFrame#topToolbar QTabBar::tab {{ padding-left: 11px; padding-right: 11px; }}
        QFrame#topToolbar QTabBar::tab:focus {{ padding-left: 10px; padding-right: 10px; }}
        QToolTip {{ background: {t['card']}; color: {t['text']};
            border: 1px solid {t['border']}; padding: 5px; }}
        QProgressBar {{ border: 1px solid {t['border']}; border-radius: 3px;
            background: {t['card']}; text-align: center; }}
        QProgressBar::chunk {{ background: {t['selection']}; border: 1px solid {t['accent']}; }}
        QLabel[tone="normal"] {{ color: {t['accent']}; }}
        QLabel[tone="error"] {{ color: {t['danger']}; }}
        QLabel[tone="warning"] {{ color: {t['warning']}; }}
        QLabel[tone="success"] {{ color: {t['success']}; }}
        QLabel[tone="muted"] {{ color: {t['muted']}; }}
    '''


class ThemeTracker(QObject):
    """Parent ownership handles destruction; close also disconnects explicitly."""
    changed = Signal()

    def __init__(self, window, override=None):
        super().__init__(window)
        self.window = window
        self.hints = QApplication.instance().styleHints()
        self.override = override
        self.system_scheme = self.hints.colorScheme()
        self.hints.colorSchemeChanged.connect(self.apply)
        if override is not None: self.set_override(override)
        else: self.apply(self.hints.colorScheme())

    def apply(self, scheme):
        if self.override is None: self.system_scheme = scheme
        name = self.override or resolve_scheme(scheme, QApplication.instance().palette())
        tokens = TOKENS[name]
        self.window.setProperty("themeName",name)
        self.window.setPalette(build_palette(tokens))
        self.window.setStyleSheet(build_stylesheet(tokens))
        self.changed.emit()

    def set_override(self, name):
        if name not in (None, 'light', 'dark'): raise ValueError('Invalid appearance')
        self.override = name
        try:
            if name is None: self.hints.unsetColorScheme()
            else: self.hints.setColorScheme(Qt.ColorScheme.Dark if name == 'dark' else Qt.ColorScheme.Light)
        except (AttributeError, RuntimeError): pass
        self.apply(self.hints.colorScheme() if name is None else self.system_scheme)

    def disconnect(self):
        self.hints.colorSchemeChanged.disconnect(self.apply)
        if self.override is not None:
            try: self.hints.unsetColorScheme()
            except (AttributeError,RuntimeError): pass
