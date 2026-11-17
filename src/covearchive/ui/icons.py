"""Small palette-tinted icons drawn with Qt, without external assets."""
import math
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor


def chrome_icon(kind, color):
    pixmap=QPixmap(72,72); pixmap.setDevicePixelRatio(3); pixmap.fill(Qt.transparent)
    painter=QPainter(pixmap); painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(color),1.7)); painter.setBrush(Qt.NoBrush)
    if kind=='moon':
        painter.drawArc(5,4,15,16,60*16,285*16)
        painter.drawArc(10,2,12,14,150*16,170*16)
    else:
        painter.drawEllipse(QPointF(12,12),5 if kind=='sun' else 6,5 if kind=='sun' else 6)
        if kind=='gear': painter.drawEllipse(QPointF(12,12),2,2)
        for index in range(8):
            angle=index*math.pi/4; inner=8 if kind=='sun' else 6; outer=10
            painter.drawLine(QPointF(12+inner*math.cos(angle),12+inner*math.sin(angle)),
                             QPointF(12+outer*math.cos(angle),12+outer*math.sin(angle)))
    painter.end(); return QIcon(pixmap)
