# -*- coding: utf-8 -*-
"""Print sheets in order from a sheet index.

Note:
When using the `Combine into one file` option,
the tool adds non-printable character u'\u200e'
(Left-To-Right Mark) at the start of the sheet names
to push Revit's interenal printing engine to sort
the sheets correctly per the drawing index order. 

Make sure your drawings indices consider this
when filtering for sheet numbers.

Shift-Click:
Shift-Clicking the tool will remove all
non-printable characters from the sheet numbers,
in case an error in the tool causes these characters
to remain.
"""
import re
import os.path as op
import codecs
from collections import namedtuple

from pyrevit import USER_DESKTOP
from pyrevit import framework
from pyrevit.framework import Windows, Drawing
from pyrevit import coreutils
from pyrevit import forms
from pyrevit import revit, DB
from pyrevit import script


__title__ = 'Print\nSheets'

logger = script.get_logger()
config = script.get_config()


# Non Printable Char
NPC = u'\u200e'
INDEX_FORMAT = '{{:0{digits}}}'


AvailableDoc = namedtuple('AvailableDoc', ['name', 'hash', 'linked'])

NamingParts = namedtuple(
    'NamingParts',
    ['index', 'index_spacer', 'number', 'number_spacer', 'name', 'ext']
    )
NamingFormat = namedtuple('NamingFormat', ['parts', 'template', 'space'])

SheetRevision = namedtuple('SheetRevision', ['number', 'desc', 'date'])


class ViewSheetListItem(forms.Reactive):
    def __init__(self, view_sheet, print_settings=None):
        self._sheet = view_sheet
        self.name = self._sheet.Name
        self.number = self._sheet.SheetNumber
        self.printable = self._sheet.CanBePrinted

        self._print_index = 0

        self._print_settings = print_settings
        self.all_print_settings = print_settings
        if self.all_print_settings:
            self._print_settings = self.all_print_settings[0]

        cur_rev = revit.query.get_current_sheet_revision(self._sheet)
        self.revision = ''
        if cur_rev:
            self.revision = SheetRevision(
                number=revit.query.get_rev_number(cur_rev),
                desc=cur_rev.Description,
                date=cur_rev.RevisionDate
            )

    @property
    def revit_sheet(self):
        return self._sheet

    @forms.reactive
    def print_settings(self):
        return self._print_settings

    @print_settings.setter
    def print_settings(self, value):
        self._print_settings = value

    @forms.reactive
    def print_index(self):
        return self._print_index

    @print_index.setter
    def print_index(self, value):
        self._print_index = value


class PrintSettingListItem(object):
    def __init__(self, print_settings=None):
        self._psettings = print_settings

    @property
    def name(self):
        if isinstance(self._psettings, DB.InSessionPrintSetting):
            return "<In Session>"
        else:
            return self._psettings.Name

    @property
    def print_settings(self):
        return self._psettings

    @property
    def print_params(self):
        if self.print_settings:
            return self.print_settings.PrintParameters

    @property
    def paper_size(self):
        try:
            if self.print_params:
                return self.print_params.PaperSize
        except Exception:
            pass

    @property
    def allows_variable_paper(self):
        return False


class VariablePaperPrintSettingListItem(PrintSettingListItem):
    def __init__(self):
        PrintSettingListItem.__init__(self, None)

    @property
    def name(self):
        return "<Variable Paper Size>"

    @property
    def allows_variable_paper(self):
        return True


class PrintSheetsWindow(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)

        self._init_psettings = None
        self._scheduled_sheets = []

        self.sheet_cat_id = \
            revit.query.get_category(DB.BuiltInCategory.OST_Sheets).Id

        self._setup_docs_list()
        self._setup_naming_formats()

    # doc and schedule
    @property
    def selected_doc(self):
        selected_doc = self.documents_cb.SelectedItem
        for open_doc in revit.docs:
            if open_doc.GetHashCode() == selected_doc.hash:
                return open_doc

    @property
    def selected_schedule(self):
        return self.schedules_cb.SelectedItem

    # ordering configs
    @property
    def reverse_print(self):
        return self.reverse_cb.IsChecked

    @property
    def combine_print(self):
        return self.combine_cb.IsChecked

    @property
    def show_placeholders(self):
        return self.placeholder_cb.IsChecked

    @property
    def index_digits(self):
        return int(self.index_slider.Value)

    @property
    def index_start(self):
        return int(self.indexstart_tb.Text or 0) 

    @property
    def include_placeholders(self):
        return self.indexspace_cb.IsChecked

    # print settings
    @property
    def selected_naming_format(self):
        return self.namingformat_cb.SelectedItem

    @property
    def selected_printer(self):
        return self.printers_cb.SelectedItem

    @property
    def selected_print_setting(self):
        return self.printsettings_cb.SelectedItem

    # sheet list
    @property
    def sheet_list(self):
        return self.sheets_lb.ItemsSource

    @sheet_list.setter
    def sheet_list(self, value):
        self.sheets_lb.ItemsSource = value

    @property
    def selected_sheets(self):
        return self.sheets_lb.SelectedItems

    @property
    def printable_sheets(self):
        return [x for x in self.sheet_list if x.printable]

    @property
    def selected_printable_sheets(self):
        return [x for x in self.selected_sheets if x.printable]

    # private utils
    def _get_schedule_text_data(self, schedule_view):
        schedule_data_file = \
            script.get_instance_data_file(str(schedule_view.Id.IntegerValue))
        vseop = DB.ViewScheduleExportOptions()
        vseop.TextQualifier = DB.ExportTextQualifier.None
        schedule_view.Export(op.dirname(schedule_data_file),
                             op.basename(schedule_data_file),
                             vseop)

        sched_data = []
        try:
            with codecs.open(schedule_data_file, 'r', 'utf_16_le') \
                    as sched_data_file:
                return [x.strip() for x in sched_data_file.readlines()]
        except Exception as open_err:
            logger.error('Error opening sheet index export: %s | %s',
                         schedule_data_file, open_err)
            return sched_data

    def _order_sheets_by_schedule_data(self, schedule_view, sheet_list):
        sched_data = self._get_schedule_text_data(schedule_view)

        if not sched_data:
            return sheet_list

        ordered_sheets_dict = {}
        for sheet in sheet_list:
            logger.debug('finding index for: %s', sheet.SheetNumber)
            for line_no, data_line in enumerate(sched_data):
                match_pattern = r'(^|.*\t){}(\t.*|$)'.format(sheet.SheetNumber)
                matches_sheet = re.match(match_pattern, data_line)
                logger.debug('match: %s', matches_sheet)
                try:
                    if matches_sheet:
                        ordered_sheets_dict[line_no] = sheet
                        break
                    if not sheet.CanBePrinted:
                        logger.debug('Sheet %s is not printable.',
                                     sheet.SheetNumber)
                except Exception:
                    continue

        sorted_keys = sorted(ordered_sheets_dict.keys())
        return [ordered_sheets_dict[x] for x in sorted_keys]

    def _get_ordered_schedule_sheets(self):
        if self.selected_doc == self.selected_schedule.Document:
            sheets = DB.FilteredElementCollector(self.selected_doc,
                                                 self.selected_schedule.Id)\
                    .OfClass(framework.get_type(DB.ViewSheet))\
                    .WhereElementIsNotElementType()\
                    .ToElements()

            return self._order_sheets_by_schedule_data(
                self.selected_schedule,
                sheets
                )
        return []

    def _is_sheet_index(self, schedule_view):
        return self.sheet_cat_id == schedule_view.Definition.CategoryId \
               and not schedule_view.IsTemplate

    def _get_sheet_index_list(self):
        schedules = DB.FilteredElementCollector(self.selected_doc)\
                      .OfClass(framework.get_type(DB.ViewSchedule))\
                      .WhereElementIsNotElementType()\
                      .ToElements()

        return [sched for sched in schedules if self._is_sheet_index(sched)]

    def _get_printmanager(self):
        try:
            return self.selected_doc.PrintManager
        except Exception as printerr:
            logger.critical('Error getting printer manager from document. '
                            'Most probably there is not a printer defined '
                            'on your system. | %s', printerr)
            return None

    def _setup_docs_list(self):
        docs = [AvailableDoc(name=revit.doc.Title,
                             hash=revit.doc.GetHashCode(),
                             linked=False)]
        docs.extend([
            AvailableDoc(name=x.Title, hash=x.GetHashCode(), linked=True)
            for x in revit.query.get_all_linkeddocs(doc=revit.doc)
        ])
        self.documents_cb.ItemsSource = docs
        self.documents_cb.SelectedIndex = 0

    def _setup_naming_formats(self):
        self.namingformat_cb.ItemsSource = [
            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer=' ',
                                           number='A1.00',
                                           number_spacer=' ',
                                           name='1ST FLOOR PLAN',
                                           ext='.pdf'),
                         template='{index} {number} {name}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer='_',
                                           number='A1.00',
                                           number_spacer=' ',
                                           name='1ST FLOOR PLAN',
                                           ext='.pdf'),
                         template='{index}_{number} {name}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer=' ',
                                           number='A1.00',
                                           number_spacer='_',
                                           name='1ST FLOOR PLAN',
                                           ext='.pdf'),
                         template='{index} {number}_{name}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer='_',
                                           number='A1.00',
                                           number_spacer='_',
                                           name='1ST FLOOR PLAN',
                                           ext='.pdf'),
                         template='{index}_{number}_{name}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer='_',
                                           number='A1.00',
                                           number_spacer='_',
                                           name='1ST_FLOOR_PLAN',
                                           ext='.pdf'),
                         template='{index}_{number}_{name}.pdf',
                         space='_'),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer=' ',
                                           number='A1.00',
                                           number_spacer='',
                                           name='',
                                           ext='.pdf'),
                         template='{index} {number}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer='_',
                                           number='A1.00',
                                           number_spacer='',
                                           name='',
                                           ext='.pdf'),
                         template='{index}_{number}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer=' ',
                                           number='',
                                           number_spacer='',
                                           name='1ST FLOOR PLAN',
                                           ext='.pdf'),
                         template='{index} {name}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer='_',
                                           number='',
                                           number_spacer='',
                                           name='1ST FLOOR PLAN',
                                           ext='.pdf'),
                         template='{index}_{name}.pdf',
                         space=' '),

            NamingFormat(parts=NamingParts(index='0001',
                                           index_spacer='_',
                                           number='',
                                           number_spacer='',
                                           name='1ST_FLOOR_PLAN',
                                           ext='.pdf'),
                         template='{index}_{name}.pdf',
                         space='_'),
        ]
        self.namingformat_cb.SelectedIndex = 0

    def _setup_printers(self):
        printers = list(Drawing.Printing.PrinterSettings.InstalledPrinters)
        self.printers_cb.ItemsSource = printers
        print_mgr = self._get_printmanager()
        self.printers_cb.SelectedItem = print_mgr.PrinterName

    def _setup_print_settings(self):
        if not self.selected_doc.IsLinked:
            print_settings = [VariablePaperPrintSettingListItem()]
        else:
            print_settings = []

        print_settings.extend(
            [PrintSettingListItem(self.selected_doc.GetElement(x))
             for x in self.selected_doc.GetPrintSettingIds()]
            )
        self.printsettings_cb.ItemsSource = print_settings

        print_mgr = self._get_printmanager()
        if isinstance(print_mgr.PrintSetup.CurrentPrintSetting,
                      DB.InSessionPrintSetting):
            in_session = PrintSettingListItem(
                print_mgr.PrintSetup.CurrentPrintSetting
                )
            print_settings.append(in_session)
            self.printsettings_cb.SelectedItem = in_session
        else:
            self._init_psettings = print_mgr.PrintSetup.CurrentPrintSetting
            cur_psetting_name = print_mgr.PrintSetup.CurrentPrintSetting.Name
            for psetting in print_settings:
                if psetting.name == cur_psetting_name:
                    self.printsettings_cb.SelectedItem = psetting

        if self.selected_doc.IsLinked:
            self.disable_element(self.printsettings_cb)
        else:
            self.enable_element(self.printsettings_cb)

        self._update_combine_option()

    def _update_combine_option(self):
        self.enable_element(self.combine_cb)
        if self.selected_doc.IsLinked \
                or ((self.selected_schedule and self.selected_print_setting) 
                    and self.selected_print_setting.allows_variable_paper):
            self.disable_element(self.combine_cb)
            self.combine_cb.IsChecked = False

    def _setup_sheet_list(self):
        self.schedules_cb.ItemsSource = self._get_sheet_index_list()
        self.schedules_cb.SelectedIndex = 0
        if self.schedules_cb.ItemsSource:
            self.enable_element(self.schedules_cb)
        else:
            self.disable_element(self.schedules_cb)

    def _print_combined_sheets_in_order(self, target_sheets):
        # make sure we can access the print config
        print_mgr = self._get_printmanager()
        with revit.TransactionGroup('Print Sheets in Order',
                                    doc=self.selected_doc):
            if not print_mgr:
                return
            with revit.Transaction('Set Printer Settings',
                                   doc=self.selected_doc):
                print_mgr.PrintSetup.CurrentPrintSetting = \
                    self.selected_print_setting.print_settings
                print_mgr.SelectNewPrintDriver(self.selected_printer)
                print_mgr.PrintRange = DB.PrintRange.Select
            # add non-printable char in front of sheet Numbers
            # to push revit to sort them per user
            sheet_set = DB.ViewSet()
            original_sheetnums = []
            with revit.Transaction('Fix Sheet Numbers',
                                   doc=self.selected_doc):
                for idx, sheet in enumerate(target_sheets):
                    rvtsheet = sheet.revit_sheet
                    original_sheetnums.append(rvtsheet.SheetNumber)
                    rvtsheet.SheetNumber = \
                        NPC * (idx + 1) + rvtsheet.SheetNumber
                    if sheet.printable:
                        sheet_set.Insert(rvtsheet)

            # Collect existing sheet sets
            cl = DB.FilteredElementCollector(self.selected_doc)
            viewsheetsets = cl.OfClass(framework.get_type(DB.ViewSheetSet))\
                              .WhereElementIsNotElementType()\
                              .ToElements()
            all_viewsheetsets = {vss.Name: vss for vss in viewsheetsets}

            sheetsetname = 'OrderedPrintSet'

            with revit.Transaction('Remove Previous Print Set',
                                   doc=self.selected_doc):
                # Delete existing matching sheet set
                if sheetsetname in all_viewsheetsets:
                    print_mgr.ViewSheetSetting.CurrentViewSheetSet = \
                        all_viewsheetsets[sheetsetname]
                    print_mgr.ViewSheetSetting.Delete()

            with revit.Transaction('Update Ordered Print Set',
                                   doc=self.selected_doc):
                try:
                    viewsheet_settings = print_mgr.ViewSheetSetting
                    viewsheet_settings.CurrentViewSheetSet.Views = \
                        sheet_set
                    viewsheet_settings.SaveAs(sheetsetname)
                except Exception as viewset_err:
                    sheet_report = ''
                    for sheet in sheet_set:
                        sheet_report += '{} {}\n'.format(
                            sheet.SheetNumber if isinstance(sheet,
                                                            DB.ViewSheet)
                            else '---',
                            type(sheet)
                            )
                    logger.critical(
                        'Error setting sheet set on print mechanism. '
                        'These items are included in the viewset '
                        'object:\n%s', sheet_report
                        )
                    raise viewset_err

            # set print job configurations
            print_mgr.PrintOrderReverse = self.reverse_print
            try:
                print_mgr.CombinedFile = True
            except Exception as e:
                forms.alert(str(e) +
                            '\nSet printer correctly in Print settings.')
                script.exit()
            print_mgr.PrintToFile = True
            print_mgr.PrintToFileName = \
                op.join(r'C:\\', 'Ordered Sheet Set.pdf')
            print_mgr.Apply()
            print_mgr.SubmitPrint()

            # now fix the sheet names
            with revit.Transaction('Restore Sheet Numbers',
                                   doc=self.selected_doc):
                for sheet, sheetnum in zip(target_sheets,
                                           original_sheetnums):
                    rvtsheet = sheet.revit_sheet
                    rvtsheet.SheetNumber = sheetnum

    def _print_sheets_in_order(self, target_sheets):
        # make sure we can access the print config
        print_mgr = self._get_printmanager()
        if not print_mgr:
            return
        print_mgr.PrintToFile = True
        per_sheet_psettings = self.selected_print_setting.allows_variable_paper
        with revit.DryTransaction('Set Printer Settings',
                                  doc=self.selected_doc):
            if not per_sheet_psettings:
                print_mgr.PrintSetup.CurrentPrintSetting = \
                    self.selected_print_setting.print_settings
            print_mgr.SelectNewPrintDriver(self.selected_printer)
            print_mgr.PrintRange = DB.PrintRange.Current
            naming_fmt = self.selected_naming_format
            for sheet in target_sheets:
                if sheet.printable:
                    output_fname = \
                        coreutils.cleanup_filename(
                            naming_fmt.template.format(
                                index=sheet.print_index,
                                number=sheet.number,
                                name=sheet.name.replace(' ', naming_fmt.space)),
                            windows_safe=True
                            )
                    print_mgr.PrintToFileName = \
                        op.join(USER_DESKTOP, output_fname)

                    # set the per-sheet print settings if required
                    if per_sheet_psettings:
                        print_mgr.PrintSetup.CurrentPrintSetting = \
                            sheet.print_settings

                    print_mgr.SubmitPrint(sheet.revit_sheet)
                else:
                    logger.debug('Sheet %s is not printable. Skipping print.',
                                sheet.number)

    def _print_linked_sheets_in_order(self, target_sheets):
        # make sure we can access the print config
        print_mgr = self._get_printmanager()
        if not print_mgr:
            return
        print_mgr.PrintToFile = True
        print_mgr.SelectNewPrintDriver(self.selected_printer)
        print_mgr.PrintRange = DB.PrintRange.Current
        # setting print settings needs a transaction
        # can not be done on linked docs
        # print_mgr.PrintSetup.CurrentPrintSetting =
        naming_fmt = self.selected_naming_format
        for sheet in target_sheets:
            if sheet.printable:
                output_fname = \
                    coreutils.cleanup_filename(
                        naming_fmt.template.format(
                            index=sheet.print_index,
                            number=sheet.number,
                            name=sheet.name.replace(' ', naming_fmt.space)),
                        windows_safe=True
                        )
                print_mgr.PrintToFileName = \
                    op.join(USER_DESKTOP, output_fname)
                print_mgr.SubmitPrint(sheet.revit_sheet)
            else:
                logger.debug(
                    'Linked sheet %s is not printable. Skipping print.',
                    sheet.number
                    )

    def _update_print_indices(self, sheet_list):
        start_idx = self.index_start
        for idx, sheet in enumerate(sheet_list):
            sheet.print_index = INDEX_FORMAT\
                .format(digits=self.index_digits)\
                .format(idx + start_idx)

    def _get_sheet_printsettings(self):
        all_titleblocks = revit.query.get_elements_by_categories(
            [DB.BuiltInCategory.OST_TitleBlocks],
            doc=self.selected_doc
            )
        tblock_printsettings = {}
        sheet_printsettings = {}
        doc_printsettings = \
            revit.query.get_all_print_settings(doc=self.selected_doc)
        for tblock in all_titleblocks:
            sheet = self.selected_doc.GetElement(tblock.OwnerViewId)
            # build a unique id for this tblock
            tblock_tform = tblock.GetTotalTransform()
            tblock_tid = tblock.GetTypeId().IntegerValue
            tblock_tid = tblock_tid * 100 \
                         + tblock_tform.BasisX.X * 10 \
                         + tblock_tform.BasisX.Y
            tblock_psetting = tblock_printsettings.get(tblock_tid, None)
            if not tblock_psetting:
                tblock_psetting = \
                    revit.query.get_sheet_print_settings(tblock,
                                                         doc_printsettings)
                tblock_printsettings[tblock_tid] = tblock_psetting
            if tblock_psetting:
                sheet_printsettings[sheet.SheetNumber] = tblock_psetting
        return sheet_printsettings

    def _reset_psettings(self):
        if self._init_psettings:
            print_mgr = self._get_printmanager()
            print_mgr.PrintSetup.CurrentPrintSetting = self._init_psettings

    def _update_index_slider(self):
        index_digits = \
            int(len(str(len(self._scheduled_sheets) + self.index_start)))
        self.index_slider.Minimum = max([index_digits, 2])
        self.index_slider.Maximum = self.index_slider.Minimum + 3

    # event handlers
    def doclist_changed(self, sender, args):
        self._setup_printers()
        self._setup_print_settings()
        self._setup_sheet_list()

    def sheetlist_changed(self, sender, args):
        print_settings = None
        if self.selected_schedule and self.selected_print_setting:
            if self.selected_print_setting.allows_variable_paper:
                sheet_printsettings = self._get_sheet_printsettings()
                self.show_element(self.sheetopts_wp)
                self.show_element(self.psettingcol)
                self._scheduled_sheets = [
                    ViewSheetListItem(
                        view_sheet=x,
                        print_settings=sheet_printsettings.get(
                            x.SheetNumber,
                            None))
                    for x in self._get_ordered_schedule_sheets()
                    ]
            else:
                print_settings = self.selected_print_setting.print_settings
                self.hide_element(self.sheetopts_wp)
                self.hide_element(self.psettingcol)
                self._scheduled_sheets = [
                    ViewSheetListItem(
                        view_sheet=x,
                        print_settings=[print_settings])
                    for x in self._get_ordered_schedule_sheets()
                    ]
        self._update_combine_option()
        # self._update_index_slider()
        self.options_changed(None, None)

    def options_changed(self, sender, args):
        # update index digit range
        self._update_index_slider()

        # reverse sheet if reverse is set
        sheet_list = [x for x in self._scheduled_sheets]
        if self.reverse_print:
            sheet_list.reverse()

        if self.combine_cb.IsChecked:
            self.hide_element(self.order_sp)
            self.hide_element(self.namingformat_dp)
        else:
            self.show_element(self.order_sp)
            self.show_element(self.namingformat_dp)

        # decide whether to show the placeholders or not
        if not self.show_placeholders:
            self.indexspace_cb.IsEnabled = True
            # update print indices with placeholder sheets
            self._update_print_indices(sheet_list)
            # remove placeholders if requested
            printable_sheets = []
            for sheet in sheet_list:
                if sheet.printable:
                    printable_sheets.append(sheet)
            # update print indices without placeholder sheets
            if not self.include_placeholders:
                self._update_print_indices(printable_sheets)
            self.sheet_list = printable_sheets
        else:
            self.indexspace_cb.IsChecked = True
            self.indexspace_cb.IsEnabled = False
            # update print indices
            self._update_print_indices(sheet_list)
            # Show all sheets
            self.sheet_list = sheet_list

    def set_sheet_printsettings(self, sender, args):
        if self.selected_printable_sheets:
            psettings = forms.SelectFromList.show(
                {
                    'Matching Print Settings':
                        self.selected_printable_sheets[0].all_print_settings,
                    'All Print Settings':
                        revit.query.get_all_print_settings(
                            doc=self.selected_doc
                            )
                },
                name_attr='Name',
                group_selector_title='Print Settings:',
                default_group='Matching Print Settings',
                title='Select Print Setting',
                width=350, height=400
                )
            if psettings:
                for sheet in self.selected_printable_sheets:
                    sheet.print_settings = psettings

    def sheet_selection_changed(self, sender, args):
        if self.selected_printable_sheets:
            return self.enable_element(self.sheetopts_wp)
        self.disable_element(self.sheetopts_wp)

    def validate_index_start(self, sender, args):
        args.Handled = re.match(r'[^0-9]+', args.Text)

    def rest_index(self, sender, args):
        self.indexstart_tb.Text = '0'

    def print_sheets(self, sender, args):
        if self.sheet_list:
            selected_only = False
            if self.selected_sheets:
                opts = forms.alert(
                    "You have a series of sheets selected. Do you want to "
                    "print the selected sheets or all sheets?",
                    options=["Only Selected Sheets", "All Scheduled Sheets"]
                    )
                selected_only = opts == "Only Selected Sheets"

            target_sheets = \
                self.selected_sheets if selected_only else self.sheet_list

            if not self.combine_print:
                sheet_count = len(target_sheets)
                if sheet_count > 5:
                    if not forms.alert('Are you sure you want to print {} '
                                       'sheets individually? The process can '
                                       'not be cancelled.'.format(sheet_count),
                                       ok=False, yes=True, no=True):
                        return
            self.Close()
            if self.combine_print:
                self._print_combined_sheets_in_order(target_sheets)
            else:
                if self.selected_doc.IsLinked:
                    self._print_linked_sheets_in_order(target_sheets)
                else:
                    self._print_sheets_in_order(target_sheets)
            self._reset_psettings()


def cleanup_sheetnumbers(doc):
    sheets = revit.query.get_sheets(doc=doc)
    with revit.Transaction('Cleanup Sheet Numbers', doc=doc):
        for sheet in sheets:
            sheet.SheetNumber = sheet.SheetNumber.replace(NPC, '')


if __shiftclick__:  #pylint: disable=E0602
    docs = forms.select_open_docs()
    for doc in docs:
        cleanup_sheetnumbers(doc)
else:
    PrintSheetsWindow('PrintSheets.xaml').ShowDialog()
