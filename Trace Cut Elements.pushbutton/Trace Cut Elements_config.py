# -*- coding: utf-8 -*-
"""Shift+Click configuration for Trace Cut Elements.

Stores:
  - output_kind: "Filled Region" or "Masking Region"
  - region_type: filled-region type name (used only for Filled Region)
  - line_style: line style used for rejected-outline detail lines
"""
from pyrevit import revit, DB, forms, script
from System.IO import StringReader
from System.Windows.Markup import XamlReader


def get_option(cfg, name, default=None):
    try:
        value = cfg.get_option(name, default)
    except Exception:
        value = default
    return value if value not in (None, "") else default


def region_types(doc):
    """Return sorted filled-region type names available in the project."""
    names = []
    for t in DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType):
        p = t.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p and p.AsString():
            names.append(p.AsString())
    return sorted(set(names), key=lambda x: x.lower())


def line_styles(doc):
    """Return valid FilledRegion boundary line-style names."""
    names = []
    for eid in DB.FilledRegion.GetValidLineStyleIdsForFilledRegion(doc):
        gs = doc.GetElement(eid)
        if gs is not None and gs.Name:
            names.append(gs.Name)
    return sorted(set(names), key=lambda x: x.lower())


def set_combo(combo, value, fallback_index=0):
    if value is not None:
        for item in combo.Items:
            if str(item) == value:
                combo.SelectedItem = item
                return
    if combo.Items.Count > fallback_index:
        combo.SelectedIndex = fallback_index


XAML = r'''<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Trace Cut Elements - Configuration"
        Width="500" Height="300" MinWidth="460" MinHeight="280"
        WindowStartupLocation="CenterScreen" ResizeMode="CanResizeWithGrip"
        ShowInTaskbar="False">
    <Grid Margin="18">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <Grid.ColumnDefinitions>
            <ColumnDefinition Width="145"/>
            <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>

        <TextBlock Grid.Row="0" Grid.Column="0" Margin="0,4,12,12"
                   VerticalAlignment="Center" Text="Region kind:"/>
        <ComboBox x:Name="kindCombo" Grid.Row="0" Grid.Column="1" Margin="0,0,0,12"
                  MinHeight="26"/>

        <TextBlock x:Name="typeLabel" Grid.Row="1" Grid.Column="0" Margin="0,4,12,12"
                   VerticalAlignment="Center" Text="Region type:"/>
        <ComboBox x:Name="typeCombo" Grid.Row="1" Grid.Column="1" Margin="0,0,0,12"
                  MinHeight="26"/>

        <TextBlock Grid.Row="2" Grid.Column="0" Margin="0,4,12,12"
                   VerticalAlignment="Center" Text="Region line style:"/>
        <ComboBox x:Name="styleCombo" Grid.Row="2" Grid.Column="1" Margin="0,0,0,12"
                  MinHeight="26"/>

        <TextBlock x:Name="noteText" Grid.Row="3" Grid.Column="0" Grid.ColumnSpan="2"
                   Margin="0,4,0,14" TextWrapping="Wrap"
                   Text="Sets the boundary line style for created filled/masking regions. The same style is also used for rejected-outline review lines."/>

        <StackPanel Grid.Row="4" Grid.Column="0" Grid.ColumnSpan="2"
                    Orientation="Horizontal" HorizontalAlignment="Right">
            <Button x:Name="okButton" Width="90" Height="28" Margin="0,0,8,0"
                    IsDefault="True" Content="OK"/>
            <Button x:Name="cancelButton" Width="90" Height="28"
                    IsCancel="True" Content="Cancel"/>
        </StackPanel>
    </Grid>
</Window>'''


def load_window():
    # IronPython-safe XAML loading: XamlReader.Parse accepts the XAML string
    # directly and avoids System.Xml.XmlReader import/runtime issues.
    return XamlReader.Parse(XAML)


doc = revit.doc
cfg = script.get_config()
types = region_types(doc)
styles = line_styles(doc)

if not types:
    forms.alert("This project has no filled region types.", exitscript=True)

win = load_window()
kind_combo = win.FindName("kindCombo")
type_combo = win.FindName("typeCombo")
style_combo = win.FindName("styleCombo")
type_label = win.FindName("typeLabel")
ok_button = win.FindName("okButton")
cancel_button = win.FindName("cancelButton")

for value in ("Filled Region", "Masking Region"):
    kind_combo.Items.Add(value)
for value in types:
    type_combo.Items.Add(value)
for value in styles:
    style_combo.Items.Add(value)

set_combo(kind_combo, get_option(cfg, "output_kind", "Filled Region"))
set_combo(type_combo, get_option(cfg, "region_type", None))
set_combo(style_combo, get_option(cfg, "line_style", "Claude"))


def update_region_type_state(sender=None, args=None):
    is_filled = str(kind_combo.SelectedItem) == "Filled Region"
    type_combo.IsEnabled = is_filled
    type_label.IsEnabled = is_filled


def accept(sender, args):
    if kind_combo.SelectedItem is None:
        forms.alert("Select Filled Region or Masking Region.")
        return
    if str(kind_combo.SelectedItem) == "Filled Region" and type_combo.SelectedItem is None:
        forms.alert("Select a filled region type.")
        return
    if style_combo.SelectedItem is None:
        forms.alert("Select a line style.")
        return
    win.DialogResult = True
    win.Close()


def cancel(sender, args):
    win.DialogResult = False
    win.Close()


kind_combo.SelectionChanged += update_region_type_state
ok_button.Click += accept
cancel_button.Click += cancel
update_region_type_state()

result = win.ShowDialog()
if result:
    cfg.output_kind = str(kind_combo.SelectedItem)
    if type_combo.SelectedItem is not None:
        cfg.region_type = str(type_combo.SelectedItem)
    cfg.line_style = str(style_combo.SelectedItem)
    script.save_config()
