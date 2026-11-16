"""System-following widget colors, semantic roles and explicit theme lifecycle."""
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


TOKENS = {
    'light': dict(surface='#f3f6f7', card='#ffffff', text='#182e35',
        muted='#52656c', border='#74878e', accent='#006b62', accent_text='#ffffff',
        danger='#a82d35', warning='#805500', success='#17643c', selection='#c8e9e3',
        selection_text='#123d37', focus='#007a70', disabled='#596b72', disabled_surface='#e3e9eb'),
    'dark': dict(surface='#172328', card='#101b20', text='#edf5f6',
        muted='#b9cbd1', border='#718b94', accent='#64dbc7', accent_text='#102c28',
        danger='#ffb4b9', warning='#f3ce79', success='#9ce2b5', selection='#285c55',
        selection_text='#f1fffb', focus='#64dbc7', disabled='#8d9fa5', disabled_surface='#25343a'),
}


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
        QFrame[section="card"] {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 5px; }}
        QFrame[section="card"] QLabel, QFrame[section="card"] QCheckBox {{ background: transparent; }}
        QLabel[notice="true"] {{ background: {t['card']}; border-left: 3px solid {t['warning']}; padding: 5px 8px; }}
        QLabel[notice="true"][tone="error"] {{ border-left-color: {t['danger']}; }}
        QWidget#queuePage QPushButton {{ padding: 5px 11px; }}
        QWidget#queuePage QPushButton:focus {{ padding: 4px 10px; }}
        QLabel[heading="true"] {{ font-weight: 600; color: {t['muted']}; }}
        QWidget#libraryPage QPushButton {{ padding: 4px 8px; }}
        QWidget#libraryPage QPushButton:focus {{ padding: 3px 7px; }}
        QTableView#repositoryTable::item:selected {{ background: {t['selection']}; color: {t['selection_text']}; border-bottom: 1px solid {t['accent']}; }}
        QLabel#brandTitle {{ font-weight: 600; }}
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
            border-bottom: 2px solid {t['accent']}; padding-bottom: 6px; font-weight: 600; }}
        QTabBar::tab:focus {{ padding: 6px 16px; }}
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
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.hints = QApplication.instance().styleHints()
        self.hints.colorSchemeChanged.connect(self.apply)
        self.apply(self.hints.colorScheme())

    def apply(self, scheme):
        name = resolve_scheme(scheme, QApplication.instance().palette())
        tokens = TOKENS[name]
        self.window.setProperty("themeName",name)
        self.window.setPalette(build_palette(tokens))
        self.window.setStyleSheet(build_stylesheet(tokens))

    def disconnect(self):
        self.hints.colorSchemeChanged.disconnect(self.apply)
