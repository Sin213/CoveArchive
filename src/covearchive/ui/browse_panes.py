"""Browse composition over the existing release model and identity-keyed selection."""
import unicodedata
from math import ceil
from PySide6.QtCore import Qt, QPointF, QAbstractListModel, QModelIndex, QSize, QRect, QObject, QEvent, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QFontMetricsF, QPainter, QColor, QBrush, QPen, QTextLayout, QTextOption, QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QListView, QSplitter, QScrollArea, QStatusBar, QStyledItemDelegate,
    QStyleOptionViewItem, QStyleOptionTab, QStylePainter, QTabBar, QStyle, QFrame, QSizePolicy, QMenu, QApplication, QHeaderView)
from covearchive.ui.queue_panel import display, size_text, _safe, _safe_lines
from covearchive.build_filters import BuildRules, BuildFilterError, parse_patterns
from covearchive.ui.theme import set_tone, current_tokens, fixed_font
from covearchive.formats import FORMAT_LABELS
from covearchive.providers import display_name, parse_repository_url, canonical_url
from covearchive.github.urls import RepositoryTarget


class NavTabs(QTabBar):
    def paintEvent(self,event):
        painter=QStylePainter(self)
        for i in range(self.count()):
            option=QStyleOptionTab(); self.initStyleOption(option,i); option.state &= ~QStyle.State_HasFocus; painter.drawControl(QStyle.CE_TabBarTab,option)
        if self.hasFocus():
            painter.setPen(QPen(QColor(current_tokens(self)['focus']),2)); painter.setBrush(Qt.NoBrush); painter.drawRect(self.tabRect(self.currentIndex()).adjusted(1,1,-2,-2))


class ReleaseNavigator(QAbstractListModel):
    def __init__(self, window):
        super().__init__(window); self.window=window; self.fixed_font=fixed_font(window)

    def rowCount(self,parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.window.tree.model().rows)+1

    def keys(self,index):
        model=self.window.tree.model()
        rows=range(len(model.rows)) if index.row()==0 else (index.row()-1,)
        return set().union(*(model._keys(model.index(row,0)) for row in rows))

    def flags(self,index):
        return super().flags(index)|Qt.ItemIsUserCheckable

    def data(self,index,role=Qt.DisplayRole):
        if not index.isValid(): return None
        if role==Qt.CheckStateRole:
            keys=self.keys(index); count=len(keys & self.window.checked)
            if not keys: return None
            return Qt.Checked if keys and count==len(keys) else Qt.PartiallyChecked if count else Qt.Unchecked
        if role==Qt.FontRole: return self.fixed_font
        if role==Qt.UserRole:
            model=self.window.tree.model()
            if index.row()==0: return ('All releases',False,'',sum(len(keys) for keys in model.matches))
            row=index.row()-1; release=model.rows[row].release
            return (display(release.tag_name),release.prerelease,display((release.published_at or '-')[:10]),len(model.matches[row]))
        if role in (Qt.DisplayRole,Qt.ToolTipRole):
            if index.row()==0: return 'All releases'
            model=self.window.tree.model(); row=index.row()-1; release=model.rows[row].release
            return _safe(release.tag_name) if role==Qt.ToolTipRole else display(release.tag_name)

    def setData(self,index,value,role=Qt.EditRole):
        if role!=Qt.CheckStateRole: return False
        keys=self.keys(index)
        if value in (Qt.Checked,Qt.Checked.value): self.window.checked.update(keys)
        else: self.window.checked.difference_update(keys)
        self.window.tree.model().layoutChanged.emit(); return True


class ReleaseDelegate(QStyledItemDelegate):
    def sizeHint(self,option,index):
        return QSize(120,self.parent().fontMetrics().height()+12)

    def paint(self,painter,option,index):
        tokens=current_tokens(self.parent()); group=not index.parent().isValid()
        painter.save(); rect=base_paint(self,painter,option,index,tokens['panel'] if group else None)
        font=fixed_font(self.parent()); painter.setFont(font)
        if group:
            release=index.data(Qt.UserRole); model=index.model(); count=len(model.matches[index.row()])
            meta=display((release.published_at or '-')[:10])+' · '+(f'{count} matching' if count else 'No assets' if not release.assets else 'No matching assets')
            right=min(rect.width()//2,QFontMetrics(font).horizontalAdvance(meta)+12)
            text(painter,QRect(rect.right()-right,rect.y(),right,rect.height()),meta,tokens['muted'],Qt.AlignRight)
            left=QRect(rect.x(),rect.y(),max(0,rect.width()-right-8),rect.height())
            font.setBold(True); painter.setFont(font)
            width=min(left.width()//2,QFontMetrics(font).horizontalAdvance(display(release.tag_name))+8)
            text(painter,QRect(left.x(),left.y(),width,left.height()),display(release.tag_name),tokens['text'])
            left.adjust(width,0,0,0); painter.setFont(self.parent().font())
            if release.prerelease: left=badge(painter,left,'pre',tokens)
            text(painter,left,display(release.name or release.tag_name),tokens['muted'])
        else:
            model=index.model(); status=model.data(model.index(index.row(),5,index.parent()))
            eligible=status=='Eligible'; color=tokens['text' if eligible else 'muted']; value=index.data() or ''
            if index.column()==0 and painter.fontMetrics().horizontalAdvance(value)>rect.width():
                prefix,suffix=model.name_parts[index.parent().row()]; middle=value[len(prefix):len(value)-len(suffix) if suffix else None]
                if middle: value=('\u2026' if prefix else '')+middle+('\u2026' if suffix else '')
            if index.column()==3:
                badge(painter,rect,FORMAT_LABELS.get(value,'-'),tokens)
            elif index.column()==5:
                warning=status in ('Excluded by filename rules','Not matched by include rules')
                dot=tokens['accent' if eligible else 'warning' if warning else 'muted']
                painter.setPen(QColor(dot)); painter.setBrush(QColor(dot) if eligible or warning else Qt.NoBrush)
                painter.drawEllipse(rect.x()+2,rect.center().y()-3,6,6)
                rect.adjust(15,0,0,0); painter.setFont(self.parent().font()); text(painter,rect,value,color)
            else: text(painter,rect,value,color,Qt.AlignRight if index.column()==4 else Qt.AlignLeft,Qt.ElideMiddle if index.column()==0 else Qt.ElideRight)
        if index.column()==0 and option.state & QStyle.State_Selected: painter.fillRect(QRect(option.rect.x(),option.rect.y(),3,option.rect.height()),QColor(tokens['accent']))
        if option.state & QStyle.State_HasFocus and self.parent().hasFocus():
            painter.setPen(QPen(QColor(tokens['focus']),2)); painter.setBrush(Qt.NoBrush); painter.drawRect(option.rect.adjusted(2,2,-3,-3))
        painter.restore()


class NavigatorDelegate(QStyledItemDelegate):
    def sizeHint(self,option,index):
        return QSize(160,2*self.parent().fontMetrics().height()+10)

    def paint(self,painter,option,index):
        painter.save(); rect=base_paint(self,painter,option,index)
        tokens=current_tokens(self.parent()); tag,pre,date,count=index.data(Qt.UserRole)
        painter.setFont(fixed_font(self.parent()))
        if index.row()==0:
            text(painter,rect.adjusted(0,0,-35,0),tag,tokens['text'])
            text(painter,rect,str(count),tokens['muted'],Qt.AlignRight)
        else:
            height=self.parent().fontMetrics().height(); top=QRect(rect.x(),rect.y()+4,rect.width(),height)
            if pre:
                width=painter.fontMetrics().horizontalAdvance('pre')+12
                badge(painter,QRect(top.right()-width,top.y(),width,top.height()),'pre',tokens)
                top.adjust(0,0,-width-6,0)
            text(painter,top,tag,tokens['text'])
            bottom=QRect(rect.x(),rect.y()+height+5,rect.width(),height)
            painter.setFont(self.parent().font()); text(painter,bottom,date,tokens['muted'])
            text(painter,bottom,str(count) if count else '-',tokens['muted'],Qt.AlignRight)
        if option.state & QStyle.State_HasFocus:
            painter.setPen(QColor(tokens['focus'])); painter.setBrush(Qt.NoBrush); painter.drawRect(option.rect.adjusted(1,1,-2,-2))
        painter.restore()


def base_paint(delegate,painter,option,index,background=None):
    painted=QStyleOptionViewItem(option); delegate.initStyleOption(painted,index); painted.text=''
    if background: painted.backgroundBrush=QBrush(QColor(background))
    style=painted.widget.style(); style.drawControl(QStyle.CE_ItemViewItem,painted,painter,painted.widget)
    return style.subElementRect(QStyle.SE_ItemViewItemText,painted,painted.widget).adjusted(4,0,-6,0)


def text(painter,rect,value,color,align=Qt.AlignLeft,elide=Qt.ElideRight):
    painter.setPen(QColor(color)); value=painter.fontMetrics().elidedText(value,elide,max(0,rect.width())) if painter.fontMetrics().horizontalAdvance(value)>rect.width() else value
    painter.drawText(rect,align|Qt.AlignVCenter,value)


def badge(painter,rect,value,tokens):
    width=min(rect.width(),painter.fontMetrics().horizontalAdvance(value)+12)
    box=QRect(rect.x(),rect.center().y()-painter.fontMetrics().height()//2,width,painter.fontMetrics().height())
    painter.setPen(Qt.NoPen); painter.setBrush(QColor(tokens['hover'])); painter.drawRoundedRect(box,4,4)
    text(painter,box,value,tokens['muted'],Qt.AlignHCenter)
    return rect.adjusted(width+6,0,0,0)


def wrapped_layout(value,font,width):
    layout=QTextLayout(value,font); option=QTextOption(); option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere); layout.setTextOption(option); layout.beginLayout(); y=0
    while True:
        line=layout.createLine()
        if not line.isValid(): break
        line.setLineWidth(max(1,width)); line.setPosition(QPointF(0,y)); y+=line.height()
    layout.endLayout(); return layout


class WrappedPath(QLabel):
    def __init__(self,parent=None):
        super().__init__(parent); self.setWordWrap(True); self.setTextFormat(Qt.PlainText); self.hide()
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
    def setText(self,value):
        super().setText(value); self.setToolTip(_safe_lines(value)); self.setVisible(bool(value))
        if hasattr(self,'reveal'): QTimer.singleShot(0,self.reveal)
    def clear(self): self.setText('')
    def hasHeightForWidth(self): return True
    def heightForWidth(self,width): return int(wrapped_layout(self.text(),self.font(),width).boundingRect().height()+4) if self.text() else 0
    def sizeHint(self): return QSize(180,self.heightForWidth(self.width()))
    def minimumSizeHint(self): return QSize(0,self.heightForWidth(self.width()))
    def paintEvent(self,event):
        painter=QPainter(self); painter.setPen(self.palette().color(self.foregroundRole())); wrapped_layout(self.text(),self.font(),self.width()).draw(painter,QPointF(0,2))


class ChipLabel(QLabel):
    def minimumSizeHint(self): return QSize(0,self.sizeHint().height())


class FilterChip(ChipLabel):
    def paintEvent(self,event):
        painter=QPainter(self); option=QStyleOptionViewItem(); option.initFrom(self)
        self.style().drawPrimitive(QStyle.PE_Widget,option,painter,self)
        text(painter,self.rect().adjusted(4,0,-4,0),self.text(),current_tokens(self)['text'])


class SelectionBlock(QLabel):
    def __init__(self):
        super().__init__(); self.setProperty('selectionBlock',True); self.rows=[]; self.count=0; self.total=0
        self.setTextFormat(Qt.PlainText); self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
    def hasHeightForWidth(self): return True
    def heightForWidth(self,width):
        font=QFont(self.font()); font.setPointSizeF(font.pointSizeF()*1.5); font.setBold(True)
        hint=int(wrapped_layout('Check eligible assets or whole releases.',self.font(),width).boundingRect().height()) if not self.count else 0
        return QFontMetrics(font).height()+12+len(self.rows)*(self.fontMetrics().height()+4)+hint
    def sizeHint(self): return QSize(180,self.heightForWidth(self.width()))
    def minimumSizeHint(self): return self.sizeHint()
    def paintEvent(self,event):
        painter=QPainter(self); tokens=current_tokens(self); rect=self.rect().adjusted(0,2,0,-2)
        font=QFont(self.font()); font.setPointSizeF(font.pointSizeF()*1.5); font.setBold(True); painter.setFont(font)
        height=painter.fontMetrics().height(); top=QRect(0,0,rect.width(),height)
        text(painter,top,str(self.count),tokens['text']); width=painter.fontMetrics().horizontalAdvance(str(self.count))+8
        painter.setFont(self.font()); text(painter,top.adjusted(width,0,0,0),'asset' if self.count==1 else 'assets',tokens['muted'])
        text(painter,top,size_text(self.total),tokens['text'],Qt.AlignRight)
        painter.setFont(fixed_font(self)); y=height+8
        for tag,count,total in self.rows:
            row=QRect(0,y,rect.width(),self.fontMetrics().height()+4)
            size_width=painter.fontMetrics().horizontalAdvance(size_text(total)); count_width=painter.fontMetrics().horizontalAdvance(str(count))
            size_rect=QRect(row.right()-size_width+1,y,size_width,row.height()); count_rect=QRect(size_rect.left()-self.fontMetrics().height()-count_width,y,count_width,row.height())
            text(painter,QRect(0,y,max(0,count_rect.left()-8),row.height()),display(tag),tokens['text'])
            text(painter,count_rect,str(count),tokens['muted'],Qt.AlignRight); text(painter,size_rect,size_text(total),tokens['muted'],Qt.AlignRight); y+=row.height()
        if not self.count:
            painter.setFont(self.font()); painter.setPen(QColor(tokens['muted']))
            wrapped_layout('Check eligible assets or whole releases.',self.font(),rect.width()).draw(painter,QPointF(0,y))


def label(text=''):
    result=QLabel(text); result.setTextFormat(Qt.PlainText); result.setWordWrap(True)
    result.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); return result


def install(window):
    w=window; page=w.tabs.widget(0); layout=page.layout()
    strip=QFrame(); strip.setObjectName('filterStrip'); layout.addWidget(strip)
    filters=QVBoxLayout(strip); filters.setContentsMargins(4,2,4,2); filters.setSpacing(2)
    header=QHBoxLayout(); filters.addLayout(header)
    w.filter_toggle=QPushButton('Filters && rules'); w.filter_toggle.setCheckable(True)
    w.filter_toggle.setAccessibleName('Filters & rules'); header.addWidget(w.filter_toggle)
    w.filter_chips=[FilterChip() for _ in range(4)]; w.filter_summary=w.filter_chips[0]
    for chip in w.filter_chips:
        chip.setTextFormat(Qt.PlainText); chip.setProperty('chip',True)
        chip.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed); header.addWidget(chip)
    # The elided preview owns the free space (its policy ignores size hints) and keeps it while hidden.
    policy=w.preview.sizePolicy(); policy.setRetainSizeWhenHidden(True); w.preview.setSizePolicy(policy)
    w.preview.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
    header.addWidget(w.preview,1); header.addWidget(w.save_rules)
    filters.addWidget(w.rule_error)
    w.filter_body=QWidget(); grid=QGridLayout(w.filter_body); grid.setContentsMargins(4,2,4,2)
    for index,(extension,box) in enumerate(w.formats.items()):
        box.setText(FORMAT_LABELS[extension]+(' archive-only' if extension in ('.exe','.msi') else ''))
        grid.addWidget(box,index//4,index%4)
    grid.addWidget(w.prereleases,2,0,1,4)
    for row,(name,edit) in enumerate((('Include',w.include),('Exclude',w.exclude)),3):
        grid.addWidget(QLabel(name),row,0); grid.addWidget(edit,row,1,1,3)
    filters.addWidget(w.filter_body)
    def expanded(value):
        w.filter_body.setVisible(value)
        w.filter_toggle.setAccessibleDescription('Filters & rules '+('expanded' if value else 'collapsed'))
        w.filter_toggle.setProperty('expanded',value)
    w.filter_toggle.toggled.connect(expanded); expanded(False)
    for edit in (w.include,w.exclude): edit.textChanged.connect(lambda *_:update_chips(w))
    for box in (*w.formats.values(),w.prereleases): box.toggled.connect(lambda *_:update_chips(w))
    w.splitter=QSplitter(Qt.Horizontal); w.splitter.setHandleWidth(5); layout.addWidget(w.splitter,1)
    left=QWidget(); left_layout=QVBoxLayout(left); left_layout.setContentsMargins(4,4,4,4)
    w.repository_info=label('No repository scanned'); left_layout.addWidget(w.repository_info)
    w.repository_info.setProperty('title',True)
    w.repository_provider=label(); set_tone(w.repository_provider,'muted'); left_layout.insertWidget(0,w.repository_provider)
    w.repository_counts=label(); set_tone(w.repository_counts,'muted'); left_layout.addWidget(w.repository_counts)
    w.release_list=QListView(); w.release_list.setAccessibleName('Releases'); w.release_list.setMinimumWidth(100)
    w.release_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    w.release_list.setUniformItemSizes(True); w.release_list.setItemDelegate(NavigatorDelegate(w.release_list))
    w.release_navigation=ReleaseNavigator(w); w.release_list.setModel(w.release_navigation); left_layout.addWidget(w.release_list,1)
    w.hidden_prereleases=label(); left_layout.addWidget(w.hidden_prereleases)
    w.show_prereleases=QPushButton('Show'); w.show_prereleases.setAccessibleName('Show hidden prereleases')
    w.show_prereleases.clicked.connect(lambda:w.prereleases.setChecked(True)); left_layout.addWidget(w.show_prereleases)
    w.splitter.addWidget(left)
    center=QWidget(); center_layout=QVBoxLayout(center); center_layout.setContentsMargins(4,4,4,4)
    w.center_header=QWidget(); actions=QHBoxLayout(w.center_header); actions.setContentsMargins(0,0,0,0); center_layout.addWidget(w.center_header)
    w.releases_toggle=QPushButton('Releases'); w.releases_toggle.setCheckable(True); w.releases_toggle.setAccessibleName('Show releases')
    actions.addWidget(w.releases_toggle); w.releases_toggle.toggled.connect(left.setVisible)
    w.release_title=ChipLabel('All releases'); w.release_title.setTextFormat(Qt.PlainText); w.release_title.setProperty('title',True); w.release_title.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed); actions.addWidget(w.release_title)
    w.release_meta=type(w.status)(); w.release_meta.setAlignment(Qt.AlignLeft|Qt.AlignVCenter); set_tone(w.release_meta,'muted'); actions.addWidget(w.release_meta,1)
    for button in (w.select_matching,w.clear_selection):
        button.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed); actions.addWidget(button)
    center_layout.addWidget(w.tree,1); center_layout.addWidget(w.detail)
    w.tree.setTextElideMode(Qt.ElideMiddle); w.tree.setItemDelegate(ReleaseDelegate(w.tree)); w.splitter.addWidget(center)
    w.inspector=QScrollArea(); w.inspector.setAccessibleName('Archive inspector'); w.inspector.setWidgetResizable(True); w.inspector.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    content=QWidget(); inspector=QVBoxLayout(content); inspector.setContentsMargins(8,6,8,6)
    def heading(value):
        result=label(value.upper()); result.setProperty('inspectorHeading',True); return result
    w.selection_info=SelectionBlock(); inspector.addWidget(heading('Selection')); inspector.addWidget(w.selection_info)
    inspector.addWidget(w.held_back); inspector.addWidget(heading('Destination'))
    destination_row=QHBoxLayout(); w.root.setMinimumWidth(0); destination_row.addWidget(w.root,1); destination_row.addWidget(w.browse); inspector.addLayout(destination_row)
    w.destination.setFont(fixed_font(w)); set_tone(w.destination,'muted'); inspector.addWidget(w.destination)
    w.archive_selected.setToolTip('Archive Selected (Ctrl+Enter)'); inspector.addWidget(w.archive_selected)
    separator=QFrame(); separator.setFrameShape(QFrame.HLine); inspector.addWidget(separator)
    inspector.addWidget(heading('Git history')); inspector.addWidget(label(w.history_button.toolTip()))
    inspector.addWidget(w.history_button); inspector.addWidget(w.history_cancel)
    inspector.addWidget(w.history_status); inspector.addStretch()
    def reveal_history():
        if not w._window_closed:
            content.layout().activate(); QTimer.singleShot(0,lambda: w.inspector.ensureWidgetVisible(w.history_status,0,0) if not w._window_closed else None)
    w.history_status.reveal=reveal_history
    w.inspector.setWidget(content); w.inspector.setMinimumWidth(180); w.splitter.addWidget(w.inspector)
    w.splitter.setSizes([190,650,260]); w.splitter.setStretchFactor(1,1)
    bar=QStatusBar(); bar.setSizeGripEnabled(False); w.setStatusBar(bar)
    w.status_dot=QLabel('●'); w.status_dot.setContentsMargins(6,0,4,0); w.status_dot.setTextFormat(Qt.PlainText); bar.addWidget(w.status_dot)
    bar.addWidget(w.status,2); bar.addPermanentWidget(w.summary,1)
    w.selection_status=type(w.status)(); w.selection_status.setContentsMargins(2,0,8,0); bar.addPermanentWidget(w.selection_status,1)
    w.queue_status_summary=type(w.status)(); bar.addPermanentWidget(w.queue_status_summary,1)
    w.selection_status.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
    w.summary.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
    edge=QWidget(); edge.setFixedWidth(8); bar.addPermanentWidget(edge)
    bar.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
    w.tree.header().setSectionsClickable(True); w.tree.header().setSortIndicatorShown(False)
    def sort(column):
        previous=getattr(w,'_sort_state',None)
        order=Qt.DescendingOrder if previous==(column,Qt.AscendingOrder) else Qt.AscendingOrder
        w.tree.model().sort(column,order)
    w.tree.header().sectionClicked.connect(sort)
    w.browse_events=BrowseEvents(w)
    for view in (w.tree,w.release_list):
        view.installEventFilter(w.browse_events); view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda pos,view=view:show_menu(w,view,pos))
    for key in ('Ctrl+Return','Ctrl+Enter'):
        archive=QShortcut(QKeySequence(key),w); archive.activated.connect(lambda:w.archive_selected.click() if w.archive_selected.isEnabled() else None)
        w._navigation_shortcuts.append(archive)
    w.focused_release=None
    def focus(current,previous):
        rows=w.tree.model().rows
        w.focused_release=rows[current.row()-1].release.id if current.isValid() and current.row()>0 else None
        focus_rows(w)
    w.release_list.selectionModel().currentChanged.connect(focus)
    w.filter_body.hide(); w.history_cancel.hide()
    w.tabs.currentChanged.connect(lambda *_:status_tab(w))
    install_center(w); sync(w); selection(w); responsive(w); status_tab(w)


def focus_rows(w):
    if w.focused_release is not None or getattr(w,'_rows_focused',False):
        for row,item in enumerate(w.tree.model().rows):
            w.tree.setRowHidden(row,QModelIndex(),w.focused_release is not None and item.release.id!=w.focused_release)
    w._rows_focused=w.focused_release is not None
    chosen=next((item.release for item in w.tree.model().rows if item.release.id==w.focused_release),None)
    if chosen:
        row=next(i for i,item in enumerate(w.tree.model().rows) if item.release is chosen)
        w.tree.expand(w.tree.model().index(row,0))
    w.release_title.setText(display(chosen.name or chosen.tag_name) if chosen else 'All releases')
    w.release_meta.setText(display(f'{chosen.tag_name} · {(chosen.published_at or "-")[:10]}') if chosen else f'{count_text(len(w.tree.model().rows),"release")} · {count_text(len(w.eligible),"matching asset")}')


def sync(w):
    if not hasattr(w,'release_navigation'): return
    focused=w.focused_release; selection_model=w.release_list.selectionModel(); selection_model.blockSignals(True)
    w.release_navigation.beginResetModel(); w.release_navigation.endResetModel()
    selected=next((i+1 for i,item in enumerate(w.tree.model().rows) if item.release.id==focused),0)
    w.release_list.setCurrentIndex(w.release_navigation.index(selected,0)); selection_model.blockSignals(False)
    w.focused_release=focused
    small=sum(len(item.release.assets) for item in w.tree.model().rows)<=400
    for row in range(w.tree.model().rowCount()):
        w.tree.setFirstColumnSpanned(row,QModelIndex(),True)
        if small or row<3: w.tree.expand(w.tree.model().index(row,0))
    for column in (1,2): w.tree.setColumnHidden(column,True)
    if w.focused_release not in [item.release.id for item in w.tree.model().rows]: w.focused_release=None
    focus_rows(w)
    if w.catalog:
        repo=w.catalog.repository
        w.repository_info.setText(f'{display(repo.owner)} / {display(repo.repo)}')
        w.repository_provider.setText(display_name(repo.provider))
        w.repository_counts.setText(f'{count_text(len(w.tree.model().rows),"release")} · {len(w.eligible)} matching')
    else: w.repository_info.setText('No repository scanned'); w.repository_counts.clear(); w.repository_provider.clear()
    hidden=sum(r.prerelease for r in w.catalog.releases) if w.catalog and not w.prereleases.isChecked() else 0
    w.hidden_prereleases.setText(count_text(hidden,'prerelease')+' hidden' if hidden else '')
    w.hidden_prereleases.setVisible(bool(hidden)); w.show_prereleases.setVisible(bool(hidden))
    update_chips(w)
    responsive(w)


def update_chips(w):
    parts=[', '.join(FORMAT_LABELS[ext] for ext,box in w.formats.items() if box.isChecked()) or 'No formats',
           'Prereleases included' if w.prereleases.isChecked() else 'Prereleases hidden']
    for name,edit in (('Exclude',w.exclude),('Include',w.include)):
        if edit.text(): parts.append(name+' '+display(edit.text()))
    for index,chip in enumerate(w.filter_chips):
        full=parts[index] if index<len(parts) else ''
        chip.setText(full[:32]+'…' if index>=2 and len(full)>32 else full); chip.setToolTip(_safe_lines(full)); chip.setVisible(index<len(parts) and (index<2 or w.width()>=1000))


def responsive(w):
    if not hasattr(w,'releases_toggle'): return
    compact=w.width()<1000
    if getattr(w,'_compact',None)!=compact:
        w._compact=compact; w.releases_toggle.setChecked(not compact); w.splitter.widget(0).setVisible(not compact)
        w.splitter.setSizes([160,650,230] if compact else [190,650,260])
    w.releases_toggle.setVisible(compact); w.inspector.setMaximumWidth(230 if compact else 16777215)
    update_chips(w)
    if not hasattr(w,'_column_timer'):
        w._column_timer=QTimer(w); w._column_timer.setSingleShot(True); w._column_timer.timeout.connect(lambda:compact_columns(w))
    w._column_timer.start(0)


def column_width(font,values,padding=16):
    fm=QFontMetrics(font); precise=QFontMetricsF(font); width=ceil(max(precise.horizontalAdvance(value) for value in values))
    while any(fm.elidedText(value,Qt.ElideRight,width)!=value for value in values): width+=1
    return width+padding


def fixed_column_widths(tree):
    return column_width(fixed_font(tree),FORMAT_LABELS.values()), column_width(fixed_font(tree),('1023.9 KiB','1023.9 MiB','1023.9 GiB'))


def compact_columns(w):
    header=w.tree.header()
    header.setSectionResizeMode(0,QHeaderView.Interactive if w._compact else QHeaderView.Stretch)
    header.resizeSection(5,150 if w._compact else max(210,w.tree.fontMetrics().horizontalAdvance('Prerelease, 1000 matching assets')+16))
    for column,width in zip((3,4),fixed_column_widths(w.tree)): header.resizeSection(column,width)
    if w._compact:
        model=w.tree.model()
        statuses=[model.index(row,5,model.index(root,0)).data() for root in range(model.rowCount()) if not w.tree.isRowHidden(root,QModelIndex()) for row in range(model.rowCount(model.index(root,0)))]
        header.resizeSection(5,min(column_width(w.tree.font(),('Unsupported format',),31),column_width(w.tree.font(),statuses or ['Status'],31)))
        header.resizeSection(0,max(200,w.tree.viewport().width()-sum(header.sectionSize(i) for i in (3,4,5))))
    layout_state_card(w)


def install_center(w):
    overlay=w.tree.viewport().layout(); overlay.removeWidget(w.empty)
    w.center_overlay=QScrollArea(); w.center_overlay.setFrameShape(QFrame.NoFrame); w.center_overlay.setWidgetResizable(True); w.center_overlay.setAlignment(Qt.AlignCenter)
    w.center_card=QFrame(); column=QVBoxLayout(w.center_card); column.setContentsMargins(16,12,16,12)
    for surface in (w.center_overlay,w.center_overlay.viewport(),w.center_card): surface.setProperty('stateSurface',True)
    w.center_card.setMaximumWidth(480)
    column.addWidget(w.empty); w.empty.setProperty('stateHeading',True); set_tone(w.empty,'heading')
    w.center_description=label(); set_tone(w.center_description,'muted'); column.addWidget(w.center_description)
    w.examples_panel=QFrame(); w.examples_panel.setProperty('examplePanel',True); examples=QGridLayout(w.examples_panel); examples.setContentsMargins(10,8,10,8)
    w.example_labels=[]; w.example_urls=[]
    for provider,url in (('github','github.com/owner/repository'),('codeberg','codeberg.org/owner/repository'),('gitlab','gitlab.com/group/project')):
        example=label(display_name(provider)); set_tone(example,'muted'); examples.addWidget(example,len(w.example_labels),0)
        address=label(url); address.setFont(fixed_font(w)); examples.addWidget(address,len(w.example_labels),1)
        w.example_labels.append(example); w.example_urls.append(address)
    column.addWidget(w.examples_panel)
    w.center_shortcuts=type(w.status)('Ctrl+L Focus URL · Enter Scan · Ctrl+3 Library'); w.center_shortcuts.setWordWrap(False); w.center_shortcuts.setProperty('stateHint',True); column.addWidget(w.center_shortcuts)
    w.center_url=type(w.status)(); column.addWidget(w.center_url)
    w.busy.setObjectName('scanBusy'); w.busy.setMaximumWidth(360); w.busy.setTextVisible(False)
    column.addWidget(w.busy,0,Qt.AlignHCenter); set_tone(w.elapsed,'muted'); column.addWidget(w.elapsed)
    w.retry_scan=QPushButton('Retry Scan'); w.retry_scan.setAccessibleName('Retry Scan'); w.retry_scan.setProperty('role','primary')
    w.retry_scan.clicked.connect(w._start); column.addWidget(w.retry_scan,0,Qt.AlignHCenter)
    w.center_overlay.setWidget(w.center_card); overlay.addWidget(w.center_overlay)
    center_state(w,'empty')


def center_state(w,kind,heading=None):
    if not hasattr(w,'center_overlay'): return
    shown=kind!='results' and (kind!='result_empty' or w.tree.model().rowCount()==0)
    w.center_overlay.setVisible(shown)
    if kind=='result_empty' and not shown: w.release_meta.setText(heading or '')
    plain=kind in ('empty','scanning','error')
    w.center_header.setVisible(not plain); w.tree.header().setVisible(not plain)
    loaded=w.catalog is not None
    w.release_list.setVisible(loaded); w.repository_counts.setVisible(loaded); w.repository_provider.setVisible(loaded)
    set_tone(w.repository_info,'heading' if loaded else 'muted')
    if not loaded:
        w.repository_info.setText('Scanning releases' if kind=='scanning' else 'No repository scanned')
        w.hidden_prereleases.hide(); w.show_prereleases.hide()
    w.empty.setVisible(shown)
    w.empty.setText(heading or ('Scanning releases' if kind=='scanning' else 'No repository scanned'))
    w.center_description.setText('Runs in a separate process. Cancel Scan stops it without touching existing archives.' if kind=='scanning' else 'Paste a repository or releases URL in the toolbar and select Scan. Nothing is scanned automatically.' if kind=='empty' else '')
    w.center_description.setVisible(kind in ('empty','scanning'))
    for example in (*w.example_labels,w.center_shortcuts): example.setVisible(kind=='empty')
    w.examples_panel.setVisible(kind=='empty')
    try:
        target=parse_repository_url(w.url.text()); url=canonical_url(RepositoryTarget(target.owner,target.repo,target.provider,target.host))
    except (ValueError,TypeError,AttributeError): url=_safe_lines(w.url.text())
    w.center_url.setText(display(url) if kind in ('scanning','error') else '')
    w.center_url.setVisible(kind in ('scanning','error'))
    w.retry_scan.setVisible(kind=='error'); w.retry_scan.setEnabled(w.active_id is None)
    w.busy.setVisible(kind=='scanning'); w.elapsed.setVisible(kind=='scanning')
    layout_state_card(w); status_tab(w)


def layout_state_card(w):
    if not hasattr(w,'center_card'): return
    width=min(480,w.center_overlay.viewport().width())
    w.center_card.layout().activate()
    height=max(w.center_card.minimumSizeHint().height(),w.center_card.heightForWidth(width))
    w.center_card.setMaximumHeight(max(1,height))
    w.busy.setFixedWidth(max(0,min(360,width-32)))


def status_tab(w):
    if not hasattr(w,'queue_status_summary'): return
    tab=w.tabs.currentIndex()
    w.summary.setVisible(tab==0 and bool(w.summary.text())); w.selection_status.setVisible(tab==0)
    w.queue_status_summary.hide()


def count_text(count,noun): return f'{count} {noun}'+('' if count==1 else 's')


def selection(w):
    if not hasattr(w,'selection_info'): return
    chosen=[pair for key,pair in w.eligible.items() if key in w.checked]
    count=len(chosen); size=sum(asset.size or 0 for _,asset in chosen)
    lines=[f'{count} '+('asset' if count==1 else 'assets')+' · '+size_text(size)]
    releases={}
    for release,asset in chosen:
        entry=releases.setdefault(release.id,[release.tag_name,0,0]); entry[1]+=1; entry[2]+=asset.size or 0
    lines.extend(display(f'{tag} · {n} · {size_text(total)}') for tag,n,total in releases.values())
    if not chosen: lines.append('Check assets or whole releases. Only eligible assets can be checked.')
    w.selection_info.setText('\n'.join(lines))
    w.selection_info.rows=list(releases.values()); w.selection_info.count=count; w.selection_info.total=size
    w.selection_info.updateGeometry(); w.selection_info.update()
    w.selection_status.setText(f'{count} selected · {size_text(size)}')
    w.release_navigation.dataChanged.emit(w.release_navigation.index(0,0),w.release_navigation.index(w.release_navigation.rowCount()-1,0))


def exclude_pattern(name,existing=''):
    if not isinstance(name,str) or not name or any(c in '/\\' or unicodedata.category(c) in ('Cc','Cf','Cs','Zl','Zp') for c in name): return None
    pattern=''.join({'*':'[*]','?':'[?]','[':'[[]',',':'[,]'}.get(c,c) for c in name)
    if pattern!=pattern.strip(): return None
    try:
        parsed=parse_patterns(existing+(', ' if existing else '')+pattern)
        BuildRules((),parsed)
    except BuildFilterError: return None
    return pattern


class BrowseEvents(QObject):
    def eventFilter(self,view,event):
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Space and event.modifiers()==Qt.NoModifier:
            index=view.currentIndex()
            if index.isValid():
                index=index.siblingAtColumn(0); state=index.data(Qt.CheckStateRole)
                view.model().setData(index,Qt.Unchecked if state==Qt.Checked else Qt.Checked,Qt.CheckStateRole)
            return True
        return False


def context_menu(w,index,navigator=False):
    menu=QMenu(w); menu.setToolTipsVisible(True); model=w.tree.model()
    if navigator:
        if not index.isValid() or index.row()==0: return menu
        index=model.index(index.row()-1,0)
    if not index.isValid(): return menu
    group=not index.parent().isValid(); root=index.row() if group else index.parent().row()
    release=model.rows[root].release; parent=model.index(root,0)
    asset=None if group else model.data(index,Qt.UserRole)
    def action(value,callback):
        result=menu.addAction(_safe(value).replace('&','&&')); result.triggered.connect(lambda *_:callback()); return result
    if asset is not None:
        key=(release.id,asset.id); checked=key in w.checked
        toggle=action('Uncheck' if checked else 'Check',lambda:model.setData(index.siblingAtColumn(0),Qt.Unchecked if checked else Qt.Checked,Qt.CheckStateRole))
        toggle.setEnabled(bool(model._keys(index.siblingAtColumn(0))))
    action('Check all eligible in '+release.tag_name,lambda:model.setData(parent,Qt.Checked,Qt.CheckStateRole))
    if group:
        action('Uncheck all in '+release.tag_name,lambda:uncheck_release(w,release.id))
        action('Show only this release',lambda:w.release_list.setCurrentIndex(w.release_navigation.index(root+1,0)))
        action('Copy tag',lambda:QApplication.clipboard().setText(release.tag_name))
    else:
        action('Copy filename',lambda:QApplication.clipboard().setText(asset.name))
        pattern=exclude_pattern(asset.name,w.exclude.text())
        exclude=action('Exclude this filename...',lambda:w.exclude.setText(w.exclude.text()+(', ' if w.exclude.text() else '')+pattern))
        exclude.setEnabled(pattern is not None)
        if pattern is None: exclude.setToolTip('Cannot add an exact filename rule: remove separators/control characters or shorten the name/list (128 characters, 16 patterns).')
        status=model.data(model.index(index.row(),5,parent))
        explanation=model.data(model.index(index.row(),5,parent),Qt.ToolTipRole)
        action('Why '+status+'?',lambda:w._status(explanation))
    return menu


def uncheck_release(w,release_id):
    w.checked.difference_update(key for key in tuple(w.checked) if key[0]==release_id)
    w.tree.model().layoutChanged.emit()


def show_menu(w,view,pos):
    menu=context_menu(w,view.indexAt(pos),view is w.release_list)
    if menu.actions(): menu.exec(view.viewport().mapToGlobal(pos))
    menu.deleteLater()
