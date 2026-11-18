"""Browse structure preserves real model, selection and lifecycle semantics."""
from dataclasses import replace
from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtWidgets import QStatusBar, QScrollArea
from test_ui_window import ui, isolated_data, APP, succeed, release, asset


def test_filter_strip_and_collapsed_errors(ui):
    w,_=ui
    assert hasattr(w,'filter_toggle')
    assert not w.filter_body.isVisible() and not w.filter_toggle.isChecked()
    w.filter_toggle.click(); assert w.filter_body.isVisible()
    assert 'expanded' in w.filter_toggle.accessibleDescription()
    w.include.setText('bad/path'); w.filter_toggle.click()
    assert not w.filter_body.isVisible() and w.rule_error.isVisible()
    assert w.rule_error.text()


def test_filter_strip_match_preview_is_readable(ui):
    w,c=ui
    for _ in range(5): APP.processEvents()
    assert w.preview.isHidden() and w.save_rules.width()==w.save_rules.sizeHint().width()
    succeed(w,c,release(assets=[asset(1,'app-linux.zip'),asset(2,'app-windows.zip')]))
    w.include.setText('*linux*')
    for _ in range(5): APP.processEvents()
    assert w.preview.text()=='Rules match 1 of 2 eligible assets' and w.preview.isVisible()
    assert w.preview.width()>=w.preview.fontMetrics().horizontalAdvance(w.preview.text())
    assert w.save_rules.width()==w.save_rules.sizeHint().width()


def test_release_focus_and_spanned_groups(ui):
    w,c=ui
    succeed(w,c,release(),release(id=2,tag_name='v2',assets=[asset(2)]))
    w.select_matching.click(); checked=set(w.checked)
    assert w.tree.isFirstColumnSpanned(0,QModelIndex())
    assert [w.tree.header().isSectionHidden(i) for i in range(6)]==[False,True,True,False,False,False]
    w.release_list.setCurrentIndex(w.release_list.model().index(2,0)); APP.processEvents()
    assert w.tree.isRowHidden(0,QModelIndex()) and not w.tree.isRowHidden(1,QModelIndex())
    assert w.checked==checked and len(w.eligible)==2
    w.release_list.setCurrentIndex(w.release_list.model().index(0,0))
    assert not w.tree.isRowHidden(0,QModelIndex())


def test_inspector_selection_and_destination(ui,tmp_path):
    w,c=ui; succeed(w,c,release()); w.root.setText(str(tmp_path)); w.select_matching.click()
    assert hasattr(w,'inspector') and isinstance(w.inspector,QScrollArea)
    assert '1 asset' in w.selection_info.text() and 'v1' in w.selection_info.text()
    assert str(tmp_path) in w.destination.text()
    w.formats['.zip'].setChecked(False)
    assert '0 assets' in w.selection_info.text() and w.held_back.text()
    w.resize(820,520); APP.processEvents()
    w.inspector.ensureWidgetVisible(w.history_button); APP.processEvents()
    assert w.inspector.viewport().rect().intersects(w.history_button.geometry().translated(w.history_button.parentWidget().pos())) or w.inspector.verticalScrollBar().maximum()>0


def test_publishing_disables_cancel(ui):
    w,_=ui; w._history_pending=True
    try:
        w._history_progress('publishing'); w._history_enabled()
        assert not w.history_cancel.isEnabled()
        assert w.history_status.text()=='Publishing Git history (cannot be cancelled now)...'
    finally: w._history_pending=False


def test_statusbar_owns_messages(ui):
    w,_=ui; w._status('Real failure',True)
    assert isinstance(w.status.parentWidget(),QStatusBar)
    assert w.status.text()=='Real failure' and w.status.property('tone')=='error'
    w.preferences.writable=False; w.theme_toggle.click()
    assert "won't persist" in w.status.text()


def test_v1_literal_filter_ampersand(ui):
    w,_=ui
    assert w.filter_toggle.text()=='Filters && rules'
    assert w.filter_toggle.accessibleName()=='Filters & rules'


def test_v2_natural_sanitized_filter_chips(ui):
    from covearchive.formats import FORMAT_LABELS
    from PySide6.QtWidgets import QSizePolicy
    w,c=ui; succeed(w,c,release())
    assert 'AppImage' in w.filter_chips[0].text()
    assert w.filter_chips[1].text()=='Prereleases hidden'
    assert w.filter_chips[0].sizePolicy().horizontalPolicy()==QSizePolicy.Maximum
    w.include.setText('x'*60+'\u202e'); APP.processEvents()
    chip=w.filter_chips[2]
    assert len(chip.text())<=40 and '\\u202e' in chip.toolTip() and '\u202e' not in chip.toolTip()


def test_v3_navigator_header_and_rows(ui):
    from PySide6.QtGui import QFontDatabase
    w,c=ui; succeed(w,c,release(prerelease=False,tag_name='long-tag'*50))
    assert '\\u000a' not in w.repository_info.text()
    assert hasattr(w,'repository_counts') and '1 release' in w.repository_counts.text()
    index=w.release_navigation.index(1,0)
    assert '\\u000a' not in index.data()
    assert w.release_list.horizontalScrollBarPolicy()==Qt.ScrollBarAlwaysOff
    font=index.data(Qt.FontRole)
    preferred=next((name for name in ('Cascadia Mono','Consolas','DejaVu Sans Mono','Noto Sans Mono','Liberation Mono') if name in QFontDatabase.families()),None)
    assert preferred is None or font.family()==preferred


def test_v4_center_header_single_row(ui):
    w,c=ui; succeed(w,c,release()); APP.processEvents()
    assert w.release_title.geometry().center().y()==w.select_matching.geometry().center().y()
    assert w.select_matching.width()==w.select_matching.sizeHint().width()


def test_v5_group_band_and_elided_tooltip(ui):
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import current_tokens
    w,c=ui; succeed(w,c,release(name='Name'*200)); APP.processEvents()
    index=w.tree.model().index(0,0); rect=w.tree.visualRect(index)
    image=w.tree.viewport().grab().toImage(); ratio=image.devicePixelRatio()
    assert image.pixelColor(int((rect.right()-5)*ratio),int((rect.top()+2)*ratio))==QColor(current_tokens(w)['panel'])
    assert 'Name'*100 in w.tree.model().data(index,Qt.ToolTipRole)


def test_v6_asset_roles_and_expansion(ui):
    w,c=ui; succeed(w,c,release(assets=[asset(name='long-name'*40+'.zip'),asset(2,'other.txt')]))
    model=w.tree.model(); parent=model.index(0,0)
    assert w.tree.isExpanded(parent)
    name=model.index(0,0,parent); size=model.index(0,4,parent)
    assert name.data(Qt.ToolTipRole)==name.data() and size.data(Qt.TextAlignmentRole)&Qt.AlignRight
    assert name.data(Qt.FontRole) is not None
    assert w.tree.itemDelegate().sizeHint(w.tree.viewOptions() if hasattr(w.tree,'viewOptions') else __import__('PySide6.QtWidgets',fromlist=['QStyleOptionViewItem']).QStyleOptionViewItem(),name).height()>=w.tree.fontMetrics().height()+8
    succeed(w,c,*(release(i+1,assets=[asset(j+1) for j in range(101)]) for i in range(4)))
    assert [w.tree.isExpanded(w.tree.model().index(i,0)) for i in range(4)]==[True,True,True,False]
    w.release_list.setCurrentIndex(w.release_navigation.index(4,0)); assert w.tree.isExpanded(w.tree.model().index(3,0))


def test_v7_inspector_rows_and_wrapped_destination(ui,tmp_path):
    w,c=ui; succeed(w,c,release()); w.select_matching.click(); w.root.setText(str(tmp_path))
    assert w.root.parentWidget() is w.browse.parentWidget()
    assert abs(w.root.geometry().center().y()-w.browse.geometry().center().y())<=1
    assert w.destination.wordWrap() and w.destination.hasHeightForWidth()
    assert w.destination.textFormat()==Qt.PlainText and w.destination.toolTip()==w.destination.text()
    assert w.history_button.parentWidget() is w.history_cancel.parentWidget()
    assert w.selection_info.property('selectionBlock') is True


def test_v8_status_and_v9_splitter(ui):
    w,c=ui; succeed(w,c,release()); w.select_matching.click(); APP.processEvents()
    assert w.status_dot.contentsMargins().left()>=4
    assert hasattr(w,'selection_status') and '1 selected' in w.selection_status.text()
    assert 'QStatusBar::item' in w.styleSheet() and 'QSplitter::handle' in w.styleSheet()
    assert w.splitter.handleWidth()==5


def test_s1_sort_and_exact_archive_handoff(ui,tmp_path,monkeypatch):
    w,c=ui; succeed(w,c,release(assets=[asset(1,'z.zip',size=2),asset(2,'a.zip',size=1),asset(3,'a.zip',size=1)]),release(2))
    w.root.setText(str(tmp_path)); w.select_matching.click(); checked=set(w.checked); eligible=dict(w.eligible)
    requests=[]; monkeypatch.setattr(w.bridge,'enqueue',lambda request:requests.append(request) or True)
    w._archive(); w.enqueue_pending=False
    m=w.tree.model(); parent=m.index(0,0); m.sort(0,Qt.AscendingOrder)
    assert [m.index(i,0,parent).data(Qt.UserRole).id for i in range(3)]==[2,3,1]
    assert [row.release.id for row in m.rows]==[1,2]
    w._archive(); assert requests[0].items==requests[1].items
    assert w.checked==checked and w.eligible==eligible
    w.enqueue_pending=False; w._refresh(); m=w.tree.model()
    assert m.index(0,0,m.index(0,0)).data(Qt.UserRole).id==2
    for column in (3,4,5):
        m.sort(column,Qt.DescendingOrder)
        assert w.checked==checked and w.eligible==eligible
        assert w.tree.header().isSortIndicatorShown()
    w.release_list.setCurrentIndex(w.release_navigation.index(1,0))
    w.exclude.setText('no-match.zip'); succeed(w,c,release(assets=[asset(1,'z.zip',size=2),asset(2,'a.zip',size=1),asset(3,'a.zip',size=1)]),release(2))
    w._archive(); assert requests[-1].items==requests[0].items


@__import__('pytest').mark.parametrize('state',['CANCELLED','TIMED_OUT','FAILED'])
def test_s2_rescan_retention_failure_retry_and_removed(ui,state):
    from test_ui_window import URL
    from covearchive import scan_worker as worker
    from test_scan_worker import RID,catalog
    w,c=ui; succeed(w,c,release(assets=[asset(1),asset(2)])); w.select_matching.click()
    w.url.setText(URL); w.scan.click()
    assert w.checked=={(1,1),(1,2)} and not w.archive_selected.isEnabled()
    w.formats['.zip'].setChecked(False)
    c.finish(worker.ScanFailure(RID,'network') if state=='FAILED' else None,state)
    assert w.checked=={(1,1),(1,2)}
    w.scan.click(); c.finish(worker.decode_response(worker.encode_success(RID,catalog(release(assets=[asset(2),asset(3)]))),RID))
    assert w.checked=={(1,2)} and w.held_back.text() and not w.archive_selected.isEnabled()
    c.finish(None,'CANCELLED',rid='b'*32); assert w.checked=={(1,2)}
    w.url.setText('https://github.com/other/repo'); w.scan.click(); assert not w.checked
    c.finish(None,'CANCELLED')


def test_s3_exact_exclusion_hostile_corpus():
    from covearchive.ui import browse_panes as panes
    from covearchive.build_filters import parse_patterns,allowed,BuildRules
    assert hasattr(panes,'exclude_pattern')
    for name in ('a*.ZIP','a?.zip','a[b].zip','a,b.zip','[,].zip','é.zip','UPPER.zip','&lt;.zip','a'*255,'a/b','a\\b','x\n.zip'):
        pattern=panes.exclude_pattern(name)
        if len(name)>128 or '/' in name or '\\' in name or '\n' in name:
            assert pattern is None; continue
        patterns=parse_patterns('previous.zip, '+pattern)
        assert not allowed(BuildRules((),patterns),name)
        assert allowed(BuildRules((),patterns),name+'x')
    assert panes.exclude_pattern('new.zip',', '.join(f'{i}.zip' for i in range(16))) is None


def test_s3_menus_copy_why_and_s5_safety(ui):
    from covearchive.ui import browse_panes as panes
    w,c=ui; succeed(w,c,release(tag_name='<b>&lt;',assets=[asset(name='<b>&lt;.zip')]))
    assert hasattr(panes,'context_menu')
    m=w.tree.model(); index=m.index(0,0,m.index(0,0)); APP.clipboard().setText('unchanged')
    menu=panes.context_menu(w,index); actions={a.text():a for a in menu.actions()}
    assert APP.clipboard().text()=='unchanged'
    assert all('<' not in a.text() and '>' not in a.text() for a in menu.actions())
    actions['Copy filename'].trigger(); assert APP.clipboard().text()=='<b>&lt;.zip'
    why=next(a for a in menu.actions() if a.text().startswith('Why ')); why.trigger()
    assert 'Can be selected' in w.status.text()
    assert '<' not in index.data(Qt.ToolTipRole) and '>' not in index.data(Qt.ToolTipRole)
    actions['Exclude this filename...'].trigger(); assert not w.eligible
    group=panes.context_menu(w,w.release_navigation.index(1,0),True)
    assert group.toolTipsVisible() and all('<' not in a.text() and '>' not in a.text() for a in group.actions())
    next(a for a in group.actions() if a.text()=='Copy tag').trigger(); assert APP.clipboard().text()=='<b>&lt;'


def test_s4_space_and_archive_shortcut(ui,tmp_path,monkeypatch):
    from PySide6.QtTest import QTest
    w,c=ui; succeed(w,c,release()); w.activateWindow(); w.root.setText(str(tmp_path))
    m=w.tree.model(); w.tree.setCurrentIndex(m.index(0,0)); w.tree.setFocus()
    QTest.keyClick(w.tree,Qt.Key_Space); assert w.checked=={(1,1)}
    w.release_list.setCurrentIndex(w.release_navigation.index(1,0)); w.release_list.setFocus()
    QTest.keyClick(w.release_list,Qt.Key_Space); assert not w.checked
    calls=[]; monkeypatch.setattr(w.bridge,'enqueue',lambda request:calls.append(request) or True)
    QTest.keyClick(w.tree,Qt.Key_Return,Qt.ControlModifier); APP.processEvents(); assert not calls
    w.select_matching.click(); QTest.keyClick(w.tree,Qt.Key_Return,Qt.ControlModifier); APP.processEvents(); assert len(calls)==1


def test_s6_vertical_fill_and_header_alignment(ui):
    w,_=ui; w.resize(1280,800); APP.processEvents()
    assert w.statusBar().height()<=w.fontMetrics().height()+16
    assert w.splitter.mapTo(w,w.splitter.rect().bottomLeft()).y()>=w.statusBar().geometry().top()-12
    assert w.release_meta.alignment()&Qt.AlignLeft


def test_c1_compact_crossing_geometry_and_inspector(ui):
    w,c=ui; succeed(w,c,release()); w.resize(820,520); APP.processEvents()
    assert not w.splitter.widget(0).isVisible()
    assert w.tree.header().sectionSize(0)>=200
    assert w.releases_toggle.isVisible() and not w.releases_toggle.isChecked()
    w.releases_toggle.click(); APP.processEvents(); assert w.splitter.widget(0).isVisible()
    w.resize(850,520); APP.processEvents(); assert w.splitter.widget(0).isVisible()
    w.resize(1280,800); APP.processEvents(); assert w.splitter.widget(0).isVisible()
    w.resize(820,520); APP.processEvents(); assert not w.splitter.widget(0).isVisible()
    for control in (w.root,w.browse,w.archive_selected,w.history_button):
        w.inspector.ensureWidgetVisible(control); control.setFocus(); APP.processEvents()
        point=control.mapTo(w.inspector.viewport(),control.rect().center())
        assert w.inspector.viewport().rect().contains(point)


@__import__('pytest').mark.parametrize('width,height',[(1920,1080),(1280,800),(820,520)])
def test_c1_c3_bounds_toolbar_filters_status(ui,width,height):
    w,c=ui; succeed(w,c,release()); w.include.setText('*zip*'); w.exclude.setText('*debug*')
    w.resize(width,height); APP.processEvents()
    assert w.size().width()==width and w.size().height()==height
    if width==820: assert not w.filter_chips[2].isVisible() and not w.filter_chips[3].isVisible()
    controls=[w.url,w.scan,w.theme_toggle,w.settings_button,w.save_rules,w.selection_status]
    for control in controls:
        assert w.rect().contains(control.mapTo(w,control.rect().topLeft()))
        assert w.rect().contains(control.mapTo(w,control.rect().bottomRight()))
    assert w.selection_status.mapTo(w,w.selection_status.rect().bottomRight()).x()<=width-5
    assert hasattr(w.selection_status,'paintEvent') and w.selection_status.toolTip()
    toolbar=[w.nav,w.url.parentWidget(),w.scan,w.theme_toggle,w.settings_button]
    for i,a in enumerate(toolbar):
        for b in toolbar[i+1:]:
            assert not a.geometry().intersects(b.geometry())


def test_c2_empty_scanning_failure_retry_cancel(ui):
    from test_ui_window import URL
    from test_scan_worker import RID
    from covearchive.scan_worker import ScanFailure
    w,c=ui
    assert w.empty.text()=='No repository scanned'
    assert w.empty.isVisible() and 'Nothing is scanned automatically.' in w.center_description.text()
    assert [label.text().split(' · ')[0] for label in w.example_labels]==['GitHub','Codeberg','GitLab']
    w.url.setText(URL); w.scan.click(); APP.processEvents()
    assert w.empty.text()=='Scanning releases' and w.busy.isVisible() and w.elapsed.isVisible()
    assert w.center_url.text()==c.calls[-1]
    assert not w.retry_scan.isEnabled() and 'separate process' in w.center_description.text()
    c.finish(ScanFailure(RID,'network'),'FAILED'); APP.processEvents()
    assert w.empty.text()==w.status.text()=='Network error: connection failed'
    assert w.retry_scan.isVisible() and w.retry_scan.isEnabled()
    w.retry_scan.click(); assert len(c.calls)==2 and w.active_id is not None
    c.finish(None,'CANCELLED'); APP.processEvents()
    assert w.status.text()=='Cancelled' and w.empty.text()=='No repository scanned'
    assert not w.retry_scan.isVisible()
    w.url.setText('invalid'); w.scan.click(); APP.processEvents()
    assert w.empty.text()==w.status.text() and 'Invalid URL' in w.empty.text()


def test_c2_result_empty_heading(ui):
    w,c=ui; succeed(w,c,release()); w.include.setText('nothing')
    assert w.empty.text()=='No matching assets' and not w.empty.isVisible() and w.release_meta.text()=='No matching assets'


@__import__('pytest').mark.parametrize('width',[820,1280])
def test_n1_nav_fits_every_selected_tab(ui,width):
    from PySide6.QtWidgets import QToolButton
    w,_=ui; w.resize(width,800); APP.processEvents()
    widths=None
    for selected in range(3):
        w.tabs.setCurrentIndex(selected); APP.processEvents()
        assert not w.nav.usesScrollButtons()
        current=[w.nav.tabRect(i).width() for i in range(3)]
        if widths is None: widths=current
        assert current==widths and w.nav.width()>=w.nav.sizeHint().width()
        for index in range(3): assert w.nav.rect().contains(w.nav.tabRect(index))
        assert not any(button.isVisible() for button in w.nav.findChildren(QToolButton))
        assert not w.nav.geometry().intersects(w.url.parentWidget().geometry())


@__import__('pytest').mark.parametrize('tab,enter',[(1,False),(2,True)])
def test_n2_toolbar_scan_opens_browse(ui,tab,enter):
    from PySide6.QtTest import QTest
    from test_ui_window import URL
    w,c=ui; assert not c.calls
    w.tabs.setCurrentIndex(tab); w.url.setText(URL)
    if enter: QTest.keyClick(w.url,Qt.Key_Return)
    else: w.scan.click()
    assert len(c.calls)==1 and w.tabs.currentIndex()==0
    assert w.empty.text()=='Scanning releases' and w.empty.isVisible()
    c.finish(None,'CANCELLED')


def test_n3_tab_aware_status_tracks_real_queue(ui):
    from test_ui_queue import view,job,S
    w,c=ui; succeed(w,c,release()); w.select_matching.click()
    summary=w.summary.text(); left=w.status.text()
    w.tabs.setCurrentIndex(1); w.bridge.view_changed.emit(view(200,(job(S.QUEUED),))); APP.processEvents()
    assert not w.selection_status.isVisible() and not w.summary.isVisible()
    assert not w.queue_status_summary.isVisible()
    w.tabs.setCurrentIndex(2); APP.processEvents()
    assert not w.queue_status_summary.isVisible() and not w.selection_status.isVisible() and not w.summary.isVisible()
    w.tabs.setCurrentIndex(0); APP.processEvents()
    assert w.summary.text()==summary and w.summary.isVisible() and w.selection_status.isVisible()
    assert w.status.text()==left
    assert w.summary.alignment()&Qt.AlignRight and w.selection_status.alignment()&Qt.AlignRight


def test_n4_state_card_layout_and_navigator(ui):
    from test_ui_window import URL
    from test_scan_worker import RID
    from covearchive.scan_worker import ScanFailure
    w,c=ui; w.resize(1280,800); APP.processEvents()
    assert not w.tree.header().isVisible() and not w.select_matching.isVisible()
    assert not w.release_list.isVisible() and not w.hidden_prereleases.isVisible()
    assert w.center_card.width()<=480
    assert abs(w.center_card.geometry().center().x()-w.center_overlay.viewport().rect().center().x())<=2
    assert w.examples_panel.isVisible() and len(w.example_labels)==3
    assert not w.center_shortcuts.wordWrap()
    w.url.setText(URL); w.scan.click(); APP.processEvents()
    assert not w.tree.header().isVisible() and not w.clear_selection.isVisible()
    assert w.busy.height()<=6 and w.busy.width()<=360 and w.elapsed.property('tone')=='muted'
    c.finish(ScanFailure(RID,'network'),'FAILED'); APP.processEvents()
    assert not w.tree.header().isVisible() and not w.select_matching.isVisible()
    assert w.retry_scan.width()==w.retry_scan.sizeHint().width()
    succeed(w,c,release()); APP.processEvents()
    assert w.tree.header().isVisible() and w.select_matching.isVisible() and w.release_list.isVisible()


def test_q1_q2_natural_actions_and_headers(ui):
    from PySide6.QtWidgets import QLabel
    w,_=ui
    for tab,panel,buttons in ((1,w.queue_panel,[w.queue_panel.start,*w.queue_panel.buttons.values()]),
            (2,w.library_panel,[w.library_panel.add,w.library_panel.bulk,w.library_panel.import_button,w.library_panel.export_button,w.library_panel.new,w.library_panel.rename_collection,w.library_panel.delete,w.library_panel.move,w.library_panel.rename,w.library_panel.remove,w.library_panel.auto_button])):
        w.tabs.setCurrentIndex(tab); APP.processEvents()
        assert all(button.width()==button.sizeHint().width() for button in buttons)
        assert panel.table.horizontalHeader().defaultAlignment()&Qt.AlignLeft
    queue=w.queue_panel
    assert queue.summary.parentWidget() is queue.start.parentWidget() and queue.summary.alignment()&Qt.AlignRight
    headings=[x for x in w.library_panel.findChildren(QLabel) if x.property('heading')]
    assert {x.text() for x in headings}=={'COLLECTIONS','REPOSITORIES'}


def test_h1_hostile_browse_and_single_state_frame(ui,tmp_path):
    from PySide6.QtWidgets import QLabel,QFrame
    from covearchive.ui.browse_panes import context_menu
    w,c=ui; assert not w.center_card.property('section')=='card'
    hostile='<b>x</b>&&%s\u202e\n'+'long'*70
    succeed(w,c,release(name=hostile,tag_name=hostile,assets=[asset(name=hostile+'.zip')]))
    w.select_matching.click(); w.root.setText(str(tmp_path)); APP.processEvents()
    m=w.tree.model(); parent=m.index(0,0); child=m.index(0,0,parent)
    for index in (parent,child):
        tooltip=index.data(Qt.ToolTipRole)
        assert '<' not in tooltip and '>' not in tooltip and '\u202e' not in tooltip
        assert not any('<' in action.text() or '>' in action.text() for action in context_menu(w,index).actions())
    for text in (w.release_navigation.index(1,0).data(),w.selection_info.text(),w.destination.text(),w.summary.text()):
        assert '\u202e' not in text
    from covearchive.ui.queue_panel import display
    assert w.release_navigation.index(1,0).data()==display(hostile) and display(hostile) in w.selection_info.text()
    assert '\n' not in w.destination.text() and '\n' not in w.summary.text()
    assert all(label.textFormat()==Qt.PlainText for label in (w.empty,w.selection_info,w.destination,w.summary))
    assert w.width()==1180 and w.inspector.width()<=w.width()


def test_h2_large_catalog_bounded_widgets_and_time(ui):
    import time
    from PySide6.QtWidgets import QWidget
    w,c=ui; initial=len(w.findChildren(QWidget)); begin=time.perf_counter()
    succeed(w,c,*(release(i+1,assets=[asset(j+1,f'{j}.zip',size=j) for j in range(20)]) for i in range(200)))
    refresh=time.perf_counter()-begin; begin=time.perf_counter()
    w.formats['.zip'].setChecked(False); w.formats['.zip'].setChecked(True)
    w.tree.model().sort(0,Qt.AscendingOrder); w.release_list.setCurrentIndex(w.release_navigation.index(100,0)); w.select_matching.click(); APP.processEvents()
    elapsed=time.perf_counter()-begin
    print(f'PERFORMANCE refresh={refresh:.3f}s interactions={elapsed:.3f}s widgets_added={len(w.findChildren(QWidget))-initial}')
    assert refresh+elapsed<3 and len(w.findChildren(QWidget))-initial<30 and len(w.checked)==4000


@__import__('pytest').mark.parametrize('scale',[1.5,2])
def test_h3_scale_and_closed_connections(ui,scale):
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QPushButton
    from test_scan_process import until
    from test_ui_queue import view,job,S
    w,c=ui; succeed(w,c,release()); font=QFont(w.font()); font.setPointSizeF(font.pointSizeF()*scale)
    w.setFont(font); w.resize(1280,800)
    for tab in range(3):
        w.tabs.setCurrentIndex(tab); APP.processEvents()
        for button in w.tabs.widget(tab).findChildren(QPushButton):
            if button.isVisible(): assert button.width()>=button.sizeHint().width() and button.height()>=button.sizeHint().height()
    w.close(); until(lambda:w._window_closed); APP.processEvents()
    snapshot=(w.styleSheet(),w.nav.tabText(1),w.queue_panel.seq,w.status.text())
    APP.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark); w.bridge.view_changed.emit(view(999,(job(S.QUEUED),))); APP.processEvents()
    assert snapshot==(w.styleSheet(),w.nav.tabText(1),w.queue_panel.seq,w.status.text())
    assert not w.clock.isActive() and not w._column_timer.isActive()
    assert all(not shortcut.isEnabled() for shortcut in w._navigation_shortcuts)
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QShortcut
    assert not any(timer.isActive() for timer in w.findChildren(QTimer))
    assert not any(shortcut.isEnabled() for shortcut in w.findChildren(QShortcut))
    selected=w.tabs.currentIndex(); w.nav.currentChanged.emit((selected+1)%3); w.queue_chip.click()
    assert w.tabs.currentIndex()==selected

def test_polish_font_cache_and_format_chips(ui,monkeypatch):
    from PySide6.QtGui import QFontDatabase
    from covearchive.ui import browse_panes,theme
    w,c=ui; calls=[]; original=QFontDatabase.families
    if hasattr(theme,'_fixed_family'): theme._fixed_family.cache_clear()
    monkeypatch.setattr(QFontDatabase,'families',lambda: calls.append(1) or original())
    for _ in range(20): browse_panes.fixed_font(w)
    assert len(calls)<=1

    from PySide6.QtGui import QPixmap,QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem
    succeed(w,c,release(assets=[asset(1,'a.AppImage'),asset(2,'a.7z'),asset(3,'a.txt')]))
    labels=[]; monkeypatch.setattr(browse_panes,'badge',lambda painter,rect,value,tokens: labels.append(value) or rect)
    model=w.tree.model(); pixmap=QPixmap(200,40); painter=QPainter(pixmap)
    option=QStyleOptionViewItem(); option.widget=w.tree; option.rect=pixmap.rect()
    try:
        for row in range(3): w.tree.itemDelegate().paint(painter,option,model.index(row,3,model.index(0,0)))
    finally: painter.end()
    assert labels==['AppImage','7z','-']


@__import__('pytest').mark.parametrize('kind',['empty','scanning','error','result_empty'])
@__import__('pytest').mark.parametrize('theme',['light','dark'])
def test_polish_single_state_surface(ui,kind,theme):
    from PySide6.QtWidgets import QFrame
    from PySide6.QtCore import QPoint
    from covearchive.ui.browse_panes import center_state
    w,_=ui; w.set_appearance(theme); center_state(w,kind); APP.processEvents()
    assert w.center_card.frameShape()==QFrame.NoFrame and w.center_overlay.frameShape()==QFrame.NoFrame
    assert w.center_card.grab().toImage().pixelColor(1,1)==w.center_overlay.viewport().grab().toImage().pixelColor(1,1)
    image=w.tree.viewport().grab().toImage(); point=w.center_card.mapTo(w.tree.viewport(),QPoint(1,1)); ratio=image.devicePixelRatio()
    assert image.pixelColor(int(point.x()*ratio),int(point.y()*ratio))==image.pixelColor(int(2*ratio),int(2*ratio))


@__import__('pytest').mark.parametrize('width',[180,210,250])
def test_f1_selection_hint_height(ui,width):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QFont,QFontMetrics
    block=ui[0].selection_info; block.resize(width,200)
    font=QFont(block.font()); font.setPointSizeF(font.pointSizeF()*1.5)
    wrapped=block.fontMetrics().boundingRect(QRect(0,0,width,1000),Qt.TextWordWrap,'Check eligible assets or whole releases.').height()
    assert block.heightForWidth(width)>=QFontMetrics(font).height()+8+wrapped+4


@__import__('pytest').mark.parametrize('scale',[1,1.5,2])
def test_f2_compact_no_scroll(ui,scale):
    from PySide6.QtGui import QFont
    w,c=ui; font=QFont(w.font()); font.setPointSizeF(font.pointSizeF()*scale); w.setFont(font)
    succeed(w,c,release(assets=[asset(i+1,'aria2-1.37.0-win-64bit-build'+str(i)+'.zip',size=1048576) for i in range(6)]))
    w.resize(820,520); APP.processEvents(); APP.processEvents()
    h=w.tree.header()
    print('COMPACT',scale,w.width(),w.inspector.width(),w.tree.viewport().width(),[h.sectionSize(i) for i in (0,3,4,5)])
    assert w.inspector.width()<=230 and h.sectionSize(0)>=200
    assert w.tree.horizontalScrollBar().maximum()==0
    from covearchive.formats import FORMAT_LABELS
    from covearchive.ui.theme import fixed_font
    from PySide6.QtGui import QFontMetrics,QFontMetricsF
    assert h.sectionSize(3)>=max(QFontMetricsF(fixed_font(w.tree)).horizontalAdvance(label) for label in FORMAT_LABELS.values())+16
    assert all(QFontMetrics(fixed_font(w.tree)).elidedText(label,Qt.ElideRight,h.sectionSize(3)-16)==label for label in FORMAT_LABELS.values())


def test_f4_singular_counts(ui):
    w,c=ui; succeed(w,c,release()); w.select_matching.click()
    assert '1 release' in w.repository_counts.text() and '1 releases' not in w.repository_counts.text()
    assert '1 matching asset' in w.release_meta.text() and '1 matching assets' not in w.release_meta.text()
    assert '1 releases' not in w.summary.text() and '1 matching assets' not in w.summary.text()
    assert '1 asset' in w.selection_info.text()


@__import__('pytest').mark.parametrize('tab',[0,1,2])
def test_k1_real_tab_loop(ui,tab):
    from PySide6.QtTest import QTest
    from test_ui_queue import view,job,S
    w,c=ui; succeed(w,c,release(),release(2,prerelease=True)); w.select_matching.click(); w.filter_toggle.click(); w.resize(820 if tab==0 else 1400,800); w._queue_view(view(999,(job(S.QUEUED),))); w.tabs.setCurrentIndex(tab); w.nav.setFocus(); APP.processEvents()
    if tab==0: w.releases_toggle.click(); APP.processEvents()
    seen=[]
    for _ in range(90):
        current=w.focusWidget()
        if current in seen: assert current is w.nav; break
        assert current.isVisible() and current.isEnabled(); seen.append(current)
        QTest.keyClick(current,Qt.Key_Tab); APP.processEvents()
    else: assert False,'Focus loop did not close'
    assert w.theme_toggle in seen and w.settings_button in seen
    assert [seen.index(control) for control in (w.nav,w.url,w.scan,w.theme_toggle,w.settings_button)]==sorted(seen.index(control) for control in (w.nav,w.url,w.scan,w.theme_toggle,w.settings_button))
    if tab==0: assert w.tree in seen and seen.index(w.releases_toggle)<seen.index(w.root) and seen.index(w.show_prereleases)==seen.index(w.release_list)+1
    else: assert w.queue_chip in seen
    for expected in reversed(seen):
        QTest.keyClick(w.focusWidget(),Qt.Key_Backtab); APP.processEvents(); assert w.focusWidget() is expected
    w._queue_view(view(1000,()))


def test_ux_rows_and_root_focus(ui,monkeypatch):
    from PySide6.QtCore import QRect
    from covearchive.ui import browse_panes as panes
    w,c=ui; succeed(w,c,release(assets=[asset(1,'long-architecture-name.zip'),asset(2,'unsupported.txt')]),release(2,assets=[asset(3,'none.txt')]))
    model=w.tree.model(); checks=[model.index(1,0,model.index(0,0)).data(Qt.CheckStateRole) is None,model.index(1,0).data(Qt.CheckStateRole) is None,w.release_navigation.index(2,0).data(Qt.CheckStateRole) is None]
    w.select_matching.click(); w.root.setText('relative'); w.archive_selected.click(); checks.append(w.root.hasFocus() and w.root.selectedText()=='relative')
    for width in (180,210,250,330):
        w.selection_info.resize(width,100); w.selection_info.rows=[('very-long-tag'*10,4,int(1023.9*2**20))]; calls=[]
        original=panes.text; monkeypatch.setattr(panes,'text',lambda painter,rect,value,*args: calls.append((rect,value)))
        w.selection_info.grab(); monkeypatch.setattr(panes,'text',original)
        count=next(rect for rect,value in calls if value=='4'); size=next(rect for rect,value in calls if value=='1023.9 MiB')
        checks.append(size.left()-count.right()>=w.selection_info.fontMetrics().height())
        from PySide6.QtGui import QFontMetrics
        from covearchive.ui.theme import fixed_font
        checks.append(size.width()>=QFontMetrics(fixed_font(w.selection_info)).horizontalAdvance('1023.9 MiB'))
    for message in (w.held_back,w.history_status):
        message.setText('Publishing Git history (cannot be cancelled now)... '+('long/path/'*20)); message.show()
        for width in (180,230,330): checks.append(message.heightForWidth(width)>=message.fontMetrics().boundingRect(QRect(0,0,width,10000),Qt.TextWrapAnywhere,message.text()).height())
    checks.append(w.tree.textElideMode()==Qt.ElideMiddle); assert all(checks),checks


def test_ux_paint_focus_and_busy(ui):
    from PySide6.QtGui import QPixmap,QPainter,QColor
    from PySide6.QtWidgets import QStyleOptionViewItem,QStyle
    from covearchive.ui.theme import current_tokens,build_stylesheet
    w,c=ui; succeed(w,c,release()); w.tree.setFocus(); APP.processEvents()
    for scheme in ('light','dark'):
        w.set_appearance(scheme); t=current_tokens(w); image=QPixmap(220,40); image.fill(Qt.transparent); painter=QPainter(image)
        option=QStyleOptionViewItem(); option.widget=w.tree; option.rect=image.rect(); option.state=QStyle.State_Selected|QStyle.State_HasFocus
        w.tree.itemDelegate().paint(painter,option,w.tree.model().index(0,0,w.tree.model().index(0,0))); painter.end()
        assert image.toImage().pixelColor(0,15)==QColor(t['accent']) and image.toImage().pixelColor(2,15)==QColor(t['focus'])
        assert t['accent'] in build_stylesheet(t).split('QProgressBar#scanBusy::chunk')[1].split('}')[0]



def test_c1_late_persistent_sort(ui):
    from PySide6.QtCore import QPersistentModelIndex
    w,c=ui; succeed(w,c,release(assets=[asset(1,'z.zip',size=9),asset(2,'a.zip',size=2)])); m=w.tree.model(); parent=m.index(0,0); w.tree.setCurrentIndex(m.index(1,0,parent)); held=[]
    m.layoutAboutToBeChanged.connect(lambda:held.append(QPersistentModelIndex(next(m.index(i,0,parent) for i in range(2) if m.index(i,0,parent).data()=='z.zip'))))
    m.sort(0); assert held[0].data()=='z.zip' and w.tree.currentIndex().data()=='a.zip' and w.tree.isExpanded(parent)
    for order,expected in ((Qt.AscendingOrder,[2,9]),(Qt.DescendingOrder,[9,2])):
        m.sort(4,order); assert [m.index(i,0,parent).data(Qt.UserRole).size for i in range(2)]==expected


def test_c2_rows_remain_interactive(ui):
    from PySide6.QtTest import QTest
    from covearchive.ui.browse_panes import context_menu
    w,c=ui; succeed(w,c,release()); w.exclude.setText('*'); APP.processEvents(); index=w.tree.model().index(0,0,w.tree.model().index(0,0)); point=w.tree.visualRect(index).center()
    assert not w.center_overlay.isVisible() and w.tree.viewport().childAt(point) is None
    QTest.mouseClick(w.tree.viewport(),Qt.LeftButton,pos=point); menu=context_menu(w,index)
    next(a for a in menu.actions() if a.text()=='Why Excluded by filename rules?').trigger(); assert 'exclude pattern' in w.status.text().lower()


def test_c3_c4_c5_lifecycle(ui,monkeypatch):
    from PySide6.QtWidgets import QDialog
    from test_ui_queue import view,job
    w,_=ui
    for _ in range(4): w.settings_button.click(); w.settings_dialog.reject(); APP.processEvents()
    assert len(w.findChildren(QDialog))==1
    calls=[]; monkeypatch.setattr(w._theme.hints,'unsetColorScheme',lambda:calls.append(1)); w.set_appearance('dark'); w._theme.disconnect()
    try: assert calls
    finally: w._theme.hints.colorSchemeChanged.connect(w._theme.apply)
    panel=w.queue_panel; before=panel.seq; w.bridge.view_changed.emit(view(999,())); panel.disconnect_bridge(); APP.processEvents(); assert panel.seq==before
    panel.disconnect_bridge=lambda:None


@__import__('pytest').mark.parametrize('width',[180,230,330])
def test_l1_word_wrapping(ui,width):
    from PySide6.QtCore import QRect
    label=ui[0].history_status; label.setText('cannot be cancelled now '*5)
    assert label.heightForWidth(width)>=label.fontMetrics().boundingRect(QRect(0,0,width,10000),Qt.TextWordWrap,label.text()).height()+4
    from covearchive.ui.browse_panes import wrapped_layout
    layout=wrapped_layout(label.text(),label.font(),width)
    assert all(label.text()[layout.lineAt(i).textStart()+layout.lineAt(i).textLength()-1].isspace() for i in range(layout.lineCount()-1))
    assert wrapped_layout('C:/'+'a'*300,label.font(),width).lineCount()>1


def test_l4_nav_focus_label_pixels(ui):
    w,_=ui; rect=w.nav.tabRect(0).adjusted(8,7,-8,-7); w.url.setFocus(); APP.processEvents(); plain=w.nav.grab().toImage().copy(rect)
    w.nav.setFocus(); APP.processEvents(); assert w.nav.grab().toImage().copy(rect)==plain


@__import__('pytest').mark.parametrize('closing',['disconnect_bridge','close'])
def test_c5_queued_view_after_disconnect(ui,closing):
    from test_ui_queue import view,job
    w,_=ui; panel=w.queue_panel; before=panel.seq; w.bridge.view_changed.emit(view(999,())); getattr(panel,closing)(); panel.disconnect_bridge=lambda:None
    APP.processEvents(); assert panel.seq==before
    panel._result(None); panel._run(None)


def test_m1_uncheck_one_release(ui):
    from covearchive.ui.browse_panes import context_menu
    w,c=ui; succeed(w,c,release(),release(2,assets=[asset(2)])); w.select_matching.click(); assert w.checked=={(1,1),(2,2)}
    menu=context_menu(w,w.tree.model().index(0,0)); next(a for a in menu.actions() if a.text()=='Uncheck all in v1').trigger()
    assert w.checked=={(2,2)} and set(w.eligible)=={(1,1),(2,2)}


def test_selected_row_has_one_accent_bar(ui):
    from PySide6.QtGui import QPixmap,QPainter,QColor
    from PySide6.QtWidgets import QStyleOptionViewItem,QStyle
    from covearchive.ui.theme import current_tokens
    w,c=ui; succeed(w,c,release()); w.tree.setFocus(); APP.processEvents()
    for column in (0,3,4,5):
        image=QPixmap(220,40); image.fill(Qt.transparent); painter=QPainter(image); option=QStyleOptionViewItem(); option.widget=w.tree; option.rect=image.rect(); option.state=QStyle.State_Selected|QStyle.State_HasFocus
        w.tree.itemDelegate().paint(painter,option,w.tree.model().index(0,column,w.tree.model().index(0,0))); painter.end()
        assert all(image.toImage().pixelColor(0,y)==QColor(current_tokens(w)['accent']) for y in range(5,35))==(column==0)


@__import__('pytest').mark.parametrize('width',[180,230,330])
def test_r1_history_inspector_fits_and_reveals(ui,tmp_path,width):
    from PySide6.QtCore import QRect
    from test_ui_window import until
    w,c=ui; succeed(w,c,release()); w.root.setText(str(tmp_path)); w.select_matching.click(); w.resize(820,520); w.inspector.setFixedWidth(width); w.splitter.setSizes([0,820-width,width]); w._history_pending=True
    try:
        w._history_progress('publishing'); APP.processEvents(); APP.processEvents()
        assert w.inspector.horizontalScrollBarPolicy()==Qt.ScrollBarAlwaysOff and w.inspector.widget().minimumSizeHint().width()<=w.inspector.viewport().width()
        for button in (w.archive_selected,w.browse,w.history_cancel):
            w.inspector.ensureWidgetVisible(button,0,0); APP.processEvents(); assert w.inspector.viewport().rect().contains(QRect(button.mapTo(w.inspector.viewport(),button.rect().topLeft()),button.size()))
        for message in ('Publishing Git history (cannot be cancelled now)...','Archived Git history to '+str(tmp_path/('long-folder-'*5))):
            w.history_status.setText(message); until(lambda:w.inspector.widget().minimumSizeHint().width()<=w.inspector.viewport().width() and w.inspector.viewport().rect().contains(QRect(w.history_status.mapTo(w.inspector.viewport(),w.history_status.rect().topLeft()),w.history_status.size())))
    finally: w._history_pending=False; w._history_enabled()


def test_r2_distinguishing_painted_names(ui,monkeypatch):
    from PySide6.QtGui import QPixmap,QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem
    from covearchive.ui import browse_panes as panes
    w,c=ui; tokens=('aarch64','win-32bit','win-64bit','tar.bz2','tar.gz','tar.xz'); names=['aria2-1.37.0-'+part for part in ('aarch64-linux-android-build1.zip','win-32bit-build1.zip','win-64bit-build1.zip','src.tar.bz2','src.tar.gz','src.tar.xz')]
    extras=(["<b>"+'x'*255+chr(0x202e)+chr(10)+'.zip','\u2026'],['same'*80+'.zip']*2,['single'*60+'.zip'],['a.zip','different.7z'])
    legitimate=['x'*50+'?-arm.zip','x'*50+'-other.zip']
    for corpus,expected in [(legitimate,('?-arm','-other')),(names,tokens),(['forgejo-'+'1'*45+'.xz'+suffix for suffix in ('','.asc','.sha256')],('.xz','.asc','.sha256'))]+[(corpus,()) for corpus in extras]:
        succeed(w,c,release(assets=[asset(i+1,name) for i,name in enumerate(corpus)])); w.resize(820,520); APP.processEvents(); w.tree.header().resizeSection(0,200); painted=[]
        original=panes.text
        def capture(painter,rect,value,color,align=Qt.AlignLeft,elide=Qt.ElideRight): painted.append(painter.fontMetrics().elidedText(value,elide,rect.width())); original(painter,rect,value,color,align,elide)
        monkeypatch.setattr(panes,'text',capture)
        for row in range(len(corpus)):
            index=w.tree.model().index(row,0,w.tree.model().index(0,0)); image=QPixmap(w.tree.header().sectionSize(0),40); painter=QPainter(image); option=QStyleOptionViewItem(); option.widget=w.tree; option.rect=image.rect(); w.tree.itemDelegate().paint(painter,option,index); painter.end(); assert panes._safe(corpus[row]) in index.data(Qt.ToolTipRole)
        monkeypatch.setattr(panes,'text',original); assert not expected or len(set(painted))==len(corpus) and all(token in value for token,value in zip(expected,painted)),painted
        assert all(value.count('?')==name.count('?') for name,value in zip(corpus,painted)),painted
        if expected: assert any(value.startswith('\u2026') for value in painted),painted
        if corpus is legitimate: assert all(value.endswith('\u2026') for value in painted),painted
