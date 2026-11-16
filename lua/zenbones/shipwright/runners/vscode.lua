local function to_token_colors(spec)
	local font_style = spec.gui or ""
	if font_style == "NONE" then
		font_style = ""
	else
		font_style = font_style:gsub(",", " ")
	end

	return {
		["foreground"] = spec.fg,
		["fontStyle"] = font_style,
	}
end

local function to_table(colorscheme)
	local specs, term, metadata = unpack(colorscheme)

	local name = "Zenbones"
	if metadata.Palette ~= "zenbones" then
		name = name .. " " .. metadata.Palette:gsub("^%l", string.upper)
	end
	name = name .. " " .. metadata.Background:gsub("^%l", string.upper)
	if metadata.Ness ~= "default" then
		name = name .. " " .. metadata.Ness:gsub("^%l", string.upper)
	end

	return {
		["name"] = name,
		["type"] = metadata.Background,
		["colors"] = {
			-- Base colors: https://code.visualstudio.com/api/references/theme-color#base-colors
			["focusBorder"] = specs.DiagnosticHint.fg, -- Overall border color for focused elements. This color is only used if not overridden by a component.
			["foreground"] = specs.Normal.fg, -- Overall foreground color. This color is only used if not overridden by a component.
			["disabledForeground"] = specs.Normal.fg .. "80", -- Overall foreground for disabled elements. This color is only used if not overridden by a component.
			["widget.shadow"] = specs.StatusLine.bg, -- Shadow color of widgets such as Find/Replace inside the editor.
			["widget.border"] = specs.VertSplit.fg .. "a0", -- Border color of widgets such as Find/Replace inside the editor.
			["selection.background"] = specs.Visual.bg, -- Background color of text selections in the workbench (for input fields or text areas, does not apply to selections within the editor and the terminal).
			["descriptionForeground"] = specs.Normal.fg, -- Foreground color for description text providing additional information, for example for a label.
			["errorForeground"] = specs.ErrorMsg.fg, -- Overall foreground color for error messages (this color is only used if not overridden by a component).
			["icon.foreground"] = specs.Normal.fg .. "a0", -- The default color for icons in the workbench.
			-- ["sash.hoverBorder"] = , -- The hover border color for draggable sashes.

			-- Window border: https://code.visualstudio.com/api/references/theme-color#window-border
			["window.activeBorder"] = specs.DiagnosticHint.fg, -- Border color for the active (focused) window.
			["window.inactiveBorder"] = specs.Normal.fg, -- Border color for the inactive (unfocused) windows.

			-- Activity Bar: https://code.visualstudio.com/api/references/theme-color#activity-bar
			["activityBar.foreground"] = specs.StatusLine.fg,
			["activityBar.background"] = specs.StatusLine.bg,
			["activityBarBadge.background"] = specs.DiagnosticHint.fg,
			["activityBarBadge.foreground"] = specs.StatusLineNC.bg,

			-- Button control: https://code.visualstudio.com/api/references/theme-color#button-control
			["button.background"] = specs.StatusLine.bg, -- Button background color.
			["button.foreground"] = specs.StatusLine.fg, -- Button foreground color.
			-- ["button.border"] = , -- Button border color.
			-- ["button.hoverBackground"] = , -- Button background color when hovering.
			["button.secondaryForeground"] = specs.StatusLineNC.fg, -- Secondary button foreground color.
			["button.secondaryBackground"] = specs.StatusLineNC.bg, -- Secondary button background color.
			-- ["button.secondaryHoverBackground"] = , -- Secondary button background color when hovering.
			["checkbox.background"] = specs.StatusLine.bg, -- Background color of checkbox widget.
			["checkbox.foreground"] = specs.StatusLine.fg, -- Foreground color of checkbox widget.
			-- ["checkbox.border"] = , -- Border color of checkbox widget.


			-- Editor Groups & Tabs: https://code.visualstudio.com/api/references/theme-color#editor-groups-tabs
			["editorGroupHeader.tabsBackground"] = specs.StatusLineNC.bg,
			["tab.activeForeground"] = specs.Normal.fg,
			["tab.activeBackground"] = specs.Normal.bg,
			["tab.inactiveForeground"] = specs.StatusLine.fg .. "a0",
			["tab.inactiveBackground"] = specs.StatusLine.bg,
			["tab.border"] = specs.StatusLineNC.bg,

			-- Panel colors: https://code.visualstudio.com/api/references/theme-color#panel-colors
			["panel.background"] = specs.Normal.bg, -- Panel background color.
			["panel.border"] = specs.VertSplit.fg .. "a0", -- Panel border color to separate the panel from the editor.
			-- ["panel.dropBorder"] = , -- Drag and drop feedback color for the panel titles. Panels are shown below the editor area and contain views like output and integrated terminal.
			["panelTitle.activeBorder"] = specs.VertSplit.fg .. "a0", -- Border color for the active panel title.
			["panelTitle.activeForeground"] = specs.Normal.fg, -- Title color for the active panel.
			["panelTitle.inactiveForeground"] = specs.StatusLine.fg .. "a0", -- Title color for the inactive panel.
			-- ["panelInput.border"] = , -- Input box border for inputs in the panel.
			-- ["panelSection.border"] = , -- Panel section border color used when multiple views are stacked horizontally in the panel. Panels are shown below the editor area and contain views like output and integrated terminal.
			-- ["panelSection.dropBackground"] = , -- Drag and drop feedback color for the panel sections. The color should have transparency so that the panel sections can still shine through. Panels are shown below the editor area and contain views like output and integrated terminal.
			-- ["panelSectionHeader.background"] = , -- Panel section header background color. Panels are shown below the editor area and contain views like output and integrated terminal.
			-- ["panelSectionHeader.foreground"] = , -- Panel section header foreground color. Panels are shown below the editor area and contain views like output and integrated terminal.
			-- ["panelSectionHeader.border"] = , -- Panel section header border color used when multiple views are stacked vertically in the panel. Panels are shown below the editor area and contain views like output and integrated terminal.

			-- Status Bar colors: https://code.visualstudio.com/api/references/theme-color#status-bar-colors
			["statusBar.foreground"] = specs.StatusLineNC.fg,
			["statusBar.background"] = specs.StatusLineNC.bg,
			["statusBar.debuggingBackground"] = specs.StatusLineNC.bg,
			["statusBar.noFolderBackground"] = specs.StatusLineNC.bg,
			["statusBarItem.remoteForeground"] = specs.StatusLine.fg,
			["statusBarItem.remoteBackground"] = specs.StatusLine.bg,

			-- Editor colors: https://code.visualstudio.com/api/references/theme-color#editor-colors
			["editor.foreground"] = specs.Normal.fg, -- Editor default foreground color.
			["editor.background"] = specs.Normal.bg, -- Editor background color.
			["editor.lineHighlightBackground"] = specs.CursorLine.bg,
			["editor.selectionBackground"] = specs.Visual.bg,
			-- ["editorCursor.background"] = , -- The background color of the editor cursor. Allows customizing the color of a character overlapped by a block cursor.
			["editorCursor.foreground"] = specs.Cursor.bg, -- Color of the editor cursor.
			["editorIndentGuide.activeBackground"] = specs.VertSplit.fg .. "a0",
			["editorIndentGuide.background"] = specs.VertSplit.fg .. "50",
			["editorLineNumber.activeForeground"] = specs.CursorLineNr.fg, -- Color of editor line numbers.
			["editorLineNumber.foreground"] = specs.LineNr.fg, -- Color of editor line numbers.
			["editorRuler.foreground"] = specs.VertSplit.fg .. "a0",
			-- Find colors depend on the current find string in the Find/Replace dialog.
			["editor.findMatchBackground"] = specs.CurSearch.bg, -- Color of the current search match.
			["editor.findMatchHighlightBackground"] = specs.Search.bg .. "80", -- Color of the other search matches. The color must not be opaque so as not to hide underlying decorations.
			-- ["editor.findRangeHighlightBackground"] = specs., -- Color the range limiting the search (Enable 'Find in Selection' in the find widget). The color must not be opaque so as not to hide underlying decorations.
			-- ["editor.findMatchBorder"] = specs., -- Border color of the current search match.
			-- ["editor.findMatchHighlightBorder"] = specs., -- Border color of the other search matches.
			-- ["editor.findRangeHighlightBorder"] = specs., -- Border color the range limiting the search (Enable 'Find in Selection' in the find widget).
			-- This ruler is located beneath the scroll bar on the right edge of the editor and gives an overview of the decorations in the editor.
			-- ["editorOverviewRuler.background"] = , -- Background color of the editor overview ruler. Only used when the minimap is enabled and placed on the right side of the editor.
			-- ["editorOverviewRuler.border"] = , -- Color of the overview ruler border.
			["editorOverviewRuler.findMatchForeground"] = specs.Search.bg .. "80", -- Overview ruler marker color for find matches. The color must not be opaque so as not to hide underlying decorations.
			-- ["editorOverviewRuler.rangeHighlightForeground"] = , -- Overview ruler marker color for highlighted ranges, like by the Quick Open, Symbol in File and Find features. The color must not be opaque so as not to hide underlying decorations.
			["editorOverviewRuler.selectionHighlightForeground"] = specs.Visual.bg .. "80", -- Overview ruler marker color for selection highlights. The color must not be opaque so as not to hide underlying decorations.
			-- ["editorOverviewRuler.wordHighlightForeground"] = , -- Overview ruler marker color for symbol highlights. The color must not be opaque so as not to hide underlying decorations.
			-- ["editorOverviewRuler.wordHighlightStrongForeground"] = , -- Overview ruler marker color for write-access symbol highlights. The color must not be opaque so as not to hide underlying decorations.
			["editorOverviewRuler.modifiedForeground"] = specs.DiffChange.bg, -- Overview ruler marker color for modified content.
			["editorOverviewRuler.addedForeground"] = specs.DiffAdd.bg, -- Overview ruler marker color for added content.
			["editorOverviewRuler.deletedForeground"] = specs.DiffDelete.bg, -- Overview ruler marker color for deleted content.
			["editorOverviewRuler.errorForeground"] = specs.DiagnosticError.fg, -- Overview ruler marker color for errors.
			["editorOverviewRuler.warningForeground"] = specs.DiagnosticWarn.fg, -- Overview ruler marker color for warnings.
			["editorOverviewRuler.infoForeground"] = specs.DiagnosticInfo.fg, -- Overview ruler marker color for infos.
			-- ["editorOverviewRuler.bracketMatchForeground"] = , -- Overview ruler marker color for matching brackets.
			-- Errors and warnings:
			["editorError.foreground"] = specs.DiagnosticError.fg, -- Foreground color of error squiggles in the editor.
			-- ["editorError.border"] = , -- Border color of error boxes in the editor.
			-- ["editorError.background"] = , -- Background color of error text in the editor. The color must not be opaque so as not to hide underlying decorations.
			["editorWarning.foreground"] = specs.DiagnosticWarn.fg, -- Foreground color of warning squiggles in the editor.
			-- ["editorWarning.border"] = , -- Border color of warning boxes in the editor.
			-- ["editorWarning.background"] = , -- Background color of warning text in the editor. The color must not be opaque so as not to hide underlying decorations.
			["editorInfo.foreground"] = specs.DiagnosticInfo.fg, -- Foreground color of info squiggles in the editor.
			-- ["editorInfo.border"] = , -- Border color of info boxes in the editor.
			-- ["editorInfo.background"] = , -- Background color of info text in the editor. The color must not be opaque so as not to hide underlying decorations.
			["editorHint.foreground"] = specs.DiagnosticHint.fg, -- Foreground color of hints in the editor.
			-- ["editorHint.border"] = , -- Border color of hint boxes in the editor.
			["problemsErrorIcon.foreground"] = specs.DiagnosticError.fg, -- The color used for the problems error icon.
			["problemsWarningIcon.foreground"] = specs.DiagnosticWarn.fg, -- The color used for the problems warning icon.
			["problemsInfoIcon.foreground"] = specs.DiagnosticInfo.fg, -- The color used for the problems info icon.
			-- The gutter contains the glyph margins and the line numbers:
			-- ["editorGutter.background"] = , -- Background color of the editor gutter. The gutter contains the glyph margins and the line numbers.
			["editorGutter.modifiedBackground"] = specs.DiffChange.bg, -- Editor gutter background color for lines that are modified.
			["editorGutter.addedBackground"] = specs.DiffAdd.bg, -- Editor gutter background color for lines that are added.
			["editorGutter.deletedBackground"] = specs.DiffDelete.bg, -- Editor gutter background color for lines that are deleted.
			-- ["editorGutter.commentRangeForeground"] = , -- Editor gutter decoration color for commenting ranges.
			-- ["editorGutter.foldingControlForeground"] = , -- Color of the folding control in the editor gutter.


			-- Diff editor colors: https://code.visualstudio.com/api/references/theme-color#diff-editor-colors
			["diffEditor.insertedTextBackground"] = specs.DiffAdd.bg .. "80", -- Background color for text that got inserted. The color must not be opaque so as not to hide underlying decorations.
			-- ["diffEditor.insertedTextBorder"] = specs., -- Outline color for the text that got inserted.
			["diffEditor.removedTextBackground"] = specs.DiffDelete.bg .. "80", -- Background color for text that got removed. The color must not be opaque so as not to hide underlying decorations.
			-- ["diffEditor.removedTextBorder"] = specs., -- Outline color for text that got removed.
			-- ["diffEditor.border"] = specs., -- Border color between the two text editors.
			-- ["diffEditor.diagonalFill"] = specs., -- Color of the diff editor's diagonal fill. The diagonal fill is used in side-by-side diff views.
			-- ["diffEditor.insertedLineBackground"] = specs., -- Background color for lines that got inserted. The color must not be opaque so as not to hide underlying decorations.
			-- ["diffEditor.removedLineBackground"] = specs., -- Background color for lines that got removed. The color must not be opaque so as not to hide underlying decorations.
			-- ["diffEditorGutter.insertedLineBackground"] = specs., -- Background color for the margin where lines got inserted.
			-- ["diffEditorGutter.removedLineBackground"] = specs., -- Background color for the margin where lines got removed.
			-- ["diffEditorOverview.insertedForeground"] = specs., -- Diff overview ruler foreground for inserted content.
			-- ["diffEditorOverview.removedForeground"] = specs., -- Diff overview ruler foreground for removed content.

			-- Editor widget colors: https://code.visualstudio.com/api/references/theme-color#editor-widget-colors
			["editorWidget.foreground"] = specs.StatusLineNC.fg, -- Foreground color of editor widgets, such as find/replace.
			["editorWidget.background"] = specs.StatusLineNC.bg, -- Background color of editor widgets, such as Find/Replace.
			["editorWidget.border"] = specs.StatusLine.bg, -- Border color of the editor widget unless the widget does not contain a border or defines its own border color.
			-- ["editorWidget.resizeBorder"] = specs., -- Border color of the resize bar of editor widgets. The color is only used if the widget chooses to have a resize border and if the color is not overridden by a widget.
			-- ["editorSuggestWidget.background"] = specs., -- Background color of the suggestion widget.
			-- ["editorSuggestWidget.border"] = specs., -- Border color of the suggestion widget.
			-- ["editorSuggestWidget.foreground"] = specs., -- Foreground color of the suggestion widget.
			-- ["editorSuggestWidget.focusHighlightForeground"] = specs., -- Color of the match highlights in the suggest widget when an item is focused.
			-- ["editorSuggestWidget.highlightForeground"] = specs., -- Color of the match highlights in the suggestion widget.
			-- ["editorSuggestWidget.selectedBackground"] = specs., -- Background color of the selected entry in the suggestion widget.
			-- ["editorSuggestWidget.selectedForeground"] = specs., -- Foreground color of the selected entry in the suggest widget.
			-- ["editorSuggestWidget.selectedIconForeground"] = specs., -- Icon foreground color of the selected entry in the suggest widget.
			-- ["editorSuggestWidgetStatus.foreground"] = specs., -- Foreground color of the suggest widget status.
			["editorHoverWidget.foreground"] = specs.Normal.fg, -- Foreground color of the editor hover.
			["editorHoverWidget.background"] = specs.Pmenu.bg, -- Background color of the editor hover.
			["editorHoverWidget.border"] = specs.PmenuSel.bg, -- Border color of the editor hover.
			-- ["editorHoverWidget.highlightForeground"] = specs.PmenuSel.bg, -- Foreground color of the active item in the parameter hint.
			-- ["editorHoverWidget.statusBarBackground"] = , -- Background color of the editor hover status bar.
			-- ["editorGhostText.border"] = specs., -- Border color of the ghost text shown by inline completion providers and the suggest preview.
			-- ["editorGhostText.background"] = specs., -- Background color of the ghost text in the editor.
			-- ["editorGhostText.foreground"] = specs., -- Foreground color of the ghost text shown by inline completion providers and the suggest preview.

			-- The Debug Exception widget is a peek view that shows in the editor when debug stops at an exception.
			-- ["debugExceptionWidget.background"] = specs., -- Exception widget background color.
			-- ["debugExceptionWidget.border"] = specs., -- Exception widget border color.

			-- The editor marker view shows when navigating to errors and warnings in the editor (Go to Next Error or Warning command).
			-- ["editorMarkerNavigation.background"] = specs., -- Editor marker navigation widget background.
			-- ["editorMarkerNavigationError.background"] = specs., -- Editor marker navigation widget error color.
			-- ["editorMarkerNavigationWarning.background"] = specs., -- Editor marker navigation widget warning color.
			-- ["editorMarkerNavigationInfo.background"] = specs., -- Editor marker navigation widget info color.
			-- ["editorMarkerNavigationError.headerBackground"] = specs., -- Editor marker navigation widget error heading background.
			-- ["editorMarkerNavigationWarning.headerBackground"] = specs., -- Editor marker navigation widget warning heading background.
			-- ["editorMarkerNavigationInfo.headerBackground"] = specs., -- Editor marker navigation widget info heading background.

			-- Input control: https://code.visualstudio.com/api/references/theme-color#input-control
			["input.background"] = specs.Normal.bg, -- Input box background.
			["input.foreground"] = specs.Normal.fg, -- Input box foreground.
			["input.placeholderForeground"] = specs.Comment.fg, -- Input box foreground color for placeholder text.
			["inputOption.activeBorder"] = specs.DiagnosticHint.fg, -- Border color of activated options in input fields.
			["inputValidation.errorBorder"] = specs.DiagnosticError.fg, -- Input validation border color for error severity.
			["inputValidation.infoBorder"] = specs.DiagnosticInfo.fg, -- Input validation border color for information severity.
			["inputValidation.warningBorder"] = specs.DiagnosticWarn.fg, -- Input validation border color for warning severity.

			-- Side Bar: https://code.visualstudio.com/api/references/theme-color#side-bar
			["sideBar.background"] = specs.StatusLineNC.bg, -- Side Bar background color.
			["sideBar.foreground"] = specs.StatusLineNC.fg, -- Side Bar foreground color. The Side Bar is the container for views like Explorer and Search.
			["sideBarSectionHeader.background"] = specs.StatusLine.bg, -- Side Bar section header background color.
			["sideBarSectionHeader.foreground"] = specs.StatusLine.fg, -- Side Bar section header foreground color.

			-- Minimap: https://code.visualstudio.com/api/references/theme-color#minimap
			["minimap.findMatchHighlight"] = specs.Search.bg, -- Highlight color for matches from search within files.
			["minimap.selectionHighlight"] = specs.Visual.bg, -- Highlight color for the editor selection.
			["minimap.errorHighlight"] = specs.ErrorMsg.fg, -- Highlight color for errors within the editor.
			["minimap.warningHighlight"] = specs.DiagnosticWarn.fg, -- Highlight color for warnings within the editor.
			-- ["minimap.background"] = , -- Minimap background color.
			-- ["minimap.selectionOccurrenceHighlight"] = specs.CursorLine.bg, -- Minimap marker color for repeating editor selections.
			-- ["minimap.foregroundOpacity"] = , -- Opacity of foreground elements rendered in the minimap. For example, "#000000c0" will render the elements with 75% opacity.
			-- ["minimapSlider.background"] = , -- Minimap slider background color.
			-- ["minimapSlider.hoverBackground"] = , -- Minimap slider background color when hovering.
			-- ["minimapSlider.activeBackground"] = , -- Minimap slider background color when clicked on.
			["minimapGutter.addedBackground"] = specs.DiffAdd.bg, -- Minimap gutter color for added content.
			["minimapGutter.modifiedBackground"] = specs.DiffChange.bg, -- Minimap gutter color for modified content.
			["minimapGutter.deletedBackground"] = specs.DiffDelete.bg, -- Minimap gutter color for deleted content.

			-- Quick picker colors: https://code.visualstudio.com/api/references/theme-color#quick-picker-colors
			["pickerGroup.border"] = specs.VertSplit.fg .. "a0", -- Quick picker (Quick Open) color for grouping borders.
			["pickerGroup.foreground"] = specs.StatusLineNC.fg, -- Quick picker (Quick Open) color for grouping labels.
			["quickInput.background"] = specs.StatusLineNC.bg, -- Quick input background color. The quick input widget is the container for views like the color theme picker.
			["quickInput.foreground"] = specs.StatusLineNC.fg, -- Quick input foreground color. The quick input widget is the container for views like the color theme picker.
			["quickInputList.focusBackground"] = specs.StatusLine.bg, -- Quick picker background color for the focused item.
			["quickInputList.focusForeground"] = specs.StatusLine.fg, -- Quick picker foreground color for the focused item.
			-- ["quickInputList.focusIconForeground"] = "#FF00000", -- Quick picker icon foreground color for the focused item.
			-- ["quickInputTitle.background"] = "#FF00000", -- Quick picker title background color. The quick picker widget is the container for pickers like the Command Palette.

			-- Keybinding label colors: https://code.visualstudio.com/api/references/theme-color#keybinding-label-colors
			["keybindingLabel.background"] = specs.StatusLine.bg, -- Keybinding label background color. The keybinding label is used to represent a keyboard shortcut.
			["keybindingLabel.foreground"] = specs.StatusLine.fg, -- Keybinding label foreground color. The keybinding label is used to represent a keyboard shortcut.
			["keybindingLabel.border"] = specs.Pmenu.bg, -- Keybinding label border color. The keybinding label is used to represent a keyboard shortcut.
			["keybindingLabel.bottomBorder"] = specs.PmenuSel.bg, -- Keybinding label border bottom color. The keybinding label is used to represent a keyboard shortcut.

			-- Lists and trees
			["list.activeSelectionBackground"] = specs.PmenuSel.bg, -- List/Tree background color for the selected item when the list/tree is active.
			["list.activeSelectionForeground"] = specs.Normal.fg, -- List/Tree foreground color for the selected item when the list/tree is active.
			-- ["list.activeSelectionIconForeground"] = , -- List/Tree icon foreground color for the selected item when the list/tree is active. An active list/tree has keyboard focus, an inactive does not.
			-- ["list.dropBackground"] = , -- List/Tree drag and drop background when moving items around using the mouse.
			-- ["list.focusBackground"] = , -- List/Tree background color for the focused item when the list/tree is active.
			-- ["list.focusForeground"] = , -- List/Tree foreground color for the focused item when the list/tree is active. An active list/tree has keyboard focus, an inactive does not.
			-- ["list.focusHighlightForeground"] = , -- List/Tree foreground color of the match highlights on actively focused items when searching inside the list/tree.
			-- ["list.focusOutline"] = , -- List/Tree outline color for the focused item when the list/tree is active. An active list/tree has keyboard focus, an inactive does not.
			-- ["list.focusAndSelectionOutline"] = , -- List/Tree outline color for the focused item when the list/tree is active and selected. An active list/tree has keyboard focus, an inactive does not.
			["list.highlightForeground"] = specs.DiagnosticHint.fg, -- List/Tree foreground color of the match highlights when searching inside the list/tree.
			["list.hoverBackground"] = specs.PmenuSel.bg .. "50", -- List/Tree background when hovering over items using the mouse.
			-- ["list.hoverForeground"] = , -- List/Tree foreground when hovering over items using the mouse.
			["list.inactiveSelectionBackground"] = specs.PmenuSel.bg .. "a0", -- List/Tree background color for the selected item when the list/tree is inactive.
			-- ["list.inactiveSelectionForeground"] = , -- List/Tree foreground color for the selected item when the list/tree is inactive. An active list/tree has keyboard focus, an inactive does not.
			-- ["list.inactiveSelectionIconForeground"] = , -- List/Tree icon foreground color for the selected item when the list/tree is inactive. An active list/tree has keyboard focus, an inactive does not.
			-- ["list.inactiveFocusBackground"] = , -- List background color for the focused item when the list is inactive. An active list has keyboard focus, an inactive does not. Currently only supported in lists.
			-- ["list.inactiveFocusOutline"] = , -- List/Tree outline color for the focused item when the list/tree is inactive. An active list/tree has keyboard focus, an inactive does not.
			-- ["list.invalidItemForeground"] = , -- List/Tree foreground color for invalid items, for example an unresolved root in explorer.
			-- ["list.errorForeground"] = , -- Foreground color of list items containing errors.
			-- ["list.warningForeground"] = , -- Foreground color of list items containing warnings.
			-- ["listFilterWidget.background"] = , -- List/Tree Filter background color of typed text when searching inside the list/tree.
			-- ["listFilterWidget.outline"] = , -- List/Tree Filter Widget's outline color of typed text when searching inside the list/tree.
			-- ["listFilterWidget.noMatchesOutline"] = , -- List/Tree Filter Widget's outline color when no match is found of typed text when searching inside the list/tree.
			-- ["list.filterMatchBackground"] = , -- Background color of the filtered matches in lists and trees.
			-- ["list.filterMatchBorder"] = , -- Border color of the filtered matches in lists and trees.
			-- ["list.deemphasizedForeground"] = , -- List/Tree foreground color for items that are deemphasized.
			-- ["tree.indentGuidesStroke"] = , -- Tree Widget's stroke color for indent guides.
			-- ["tree.tableColumnsBorder"] = , -- Tree stroke color for the indentation guides.
			-- ["tree.tableOddRowsBackground"] = , -- Background color for odd table rows.

			-- Text colors: https://code.visualstudio.com/api/references/theme-color#text-colors
			-- ["textBlockQuote.background"] = , -- Background color for block quotes in text.
			-- ["textBlockQuote.border"] = , -- Border color for block quotes in text.
			-- ["textCodeBlock.background"] = , -- Background color for code blocks in text.
			["textLink.activeForeground"] = specs.DiagnosticHint.fg .. "d0", -- Foreground color for links in text when clicked on and on mouse hover.
			["textLink.foreground"] = specs.DiagnosticHint.fg, -- Foreground color for links in text.
			-- ["textPreformat.foreground"] = , -- Foreground color for preformatted text segments.
			-- ["textSeparator.foreground"] = , -- Color for text separators.

			-- Badge: https://code.visualstudio.com/api/references/theme-color#badge
			["badge.foreground"] = specs.StatusLineNC.bg, -- Badge foreground color.
			["badge.background"] = specs.DiagnosticHint.fg, -- Badge background color.

			-- Notification colors: https://code.visualstudio.com/api/references/theme-color#notification-colors
			-- ["notificationCenter.border"] = , -- Notification Center border color.
			["notificationCenterHeader.foreground"] = specs.StatusLineNC.fg, -- Notification Center header foreground color.
			["notificationCenterHeader.background"] = specs.StatusLineNC.bg, -- Notification Center header background color.
			-- ["notificationToast.border"] = , -- Notification toast border color.
			["notifications.foreground"] = specs.Normal.fg, -- Notification foreground color.
			["notifications.background"] = specs.Normal.bg, -- Notification background color.
			-- ["notifications.border"] = , -- Notification border color separating from other notifications in the Notification Center.
			["notificationLink.foreground"] = specs.DiagnosticHint.fg, -- Notification links foreground color.
			["notificationsErrorIcon.foreground"] = specs.DiagnosticError.fg, -- The color used for the notification error icon.
			["notificationsWarningIcon.foreground"] = specs.DiagnosticWarn.fg, -- The color used for the notification warning icon.
			["notificationsInfoIcon.foreground"] = specs.DiagnosticInfo.fg, -- The color used for the notification info icon.


			-- -- ["badge.background"] = specs.DiagnosticInfo.fg, -- todo
			-- ["dropdown.background"] = specs.Normal.bg, -- todo
			-- -- ["focusBorder"] = specs.Pmenu.bg, -- todo
			-- ["inputOption.activeBorder"] = "#adafb7", -- todo
			-- ["list.activeSelectionBackground"] = specs.PmenuSel.bg,
			-- -- ["list.activeSelectionForeground"] = "#6c6c6c", -- todo
			-- -- ["list.highlightForeground"] = specs.CursorLineNr.fg, -- todo
			-- -- ["list.hoverBackground"] = "#e0e0e0", -- todo
			-- -- ["list.inactiveSelectionBackground"] = "#d3dbcd", -- todo
			-- ["minimap.selectionHighlight"] = specs.Visual.bg, -- todo
			-- ["panel.background"] = specs.Normal.bg, -- todo
			-- ["peekView.border"] = specs.StatusLine.bg, -- todo
			-- ["peekViewEditor.background"] = "#f2f8fc", -- todo
			-- ["peekViewEditor.matchHighlightBackground"] = "#c2dfe3", -- todo
			-- ["peekViewResult.background"] = "#f2f8fc", -- todo
			-- ["peekViewResult.matchHighlightBackground"] = "#93c6d6", -- todo
			-- ["peekViewTitle.background"] = "#f2f8fc", -- todo
			-- -- ["pickerGroup.border"] = specs.Pmenu.bg,
			-- -- ["pickerGroup.foreground"] = specs.Pmenu.bg,
			-- ["ports.iconRunningProcessForeground"] = specs.Pmenu.bg, -- todo
			-- ["progressBar.background"] = specs.StatusLine.bg, -- todo
			-- -- ["quickInputList.focusBackground"] = "#cadeb9", -- todo
			-- ["sideBar.background"] = "#f2f2f2", -- todo
			-- ["sideBarSectionHeader.background"] = "#ede8ef", -- todo
			-- -- ["titleBar.activeBackground"] = "#c4b7d7", -- todo
			-- -- ["walkThrough.embeddedEditorBackground"] = "#00000014", -- todo
			-- -- ["welcomePage.tileBackground"] = "#f0f0f7", -- todo

			-- Scrollbar control: https://code.visualstudio.com/api/references/theme-color#scrollbar-control
			["scrollbar.shadow"] = specs.TabLine.bg, -- Scrollbar slider shadow to indicate that the view is scrolled.
			["scrollbarSlider.activeBackground"] = specs.PmenuSbar.bg, -- Scrollbar slider background color when clicked on.
			["scrollbarSlider.background"] = specs.Pmenu.bg, -- Scrollbar slider background color.
			["scrollbarSlider.hoverBackground"] = specs.PmenuSel.bg, -- Scrollbar slider background color when hovering.

			-- Integrated Terminal colors: https://code.visualstudio.com/api/references/theme-color#integrated-terminal-colors
			["terminal.ansiBlack"] = term.black, -- 'Black' ANSI color in the terminal.
			["terminal.ansiBlue"] = term.blue, -- 'Blue' ANSI color in the terminal.
			["terminal.ansiBrightBlack"] = term.bright_black, -- 'BrightBlack' ANSI color in the terminal.
			["terminal.ansiBrightBlue"] = term.bright_blue, -- 'BrightBlue' ANSI color in the terminal.
			["terminal.ansiBrightCyan"] = term.bright_cyan, -- 'BrightCyan' ANSI color in the terminal.
			["terminal.ansiBrightGreen"] = term.bright_green, -- 'BrightGreen' ANSI color in the terminal.
			["terminal.ansiBrightMagenta"] = term.bright_magenta, -- 'BrightMagenta' ANSI color in the terminal.
			["terminal.ansiBrightRed"] = term.bright_red, -- 'BrightRed' ANSI color in the terminal.
			["terminal.ansiBrightWhite"] = term.bright_white, -- 'BrightWhite' ANSI color in the terminal.
			["terminal.ansiBrightYellow"] = term.bright_yellow, -- 'BrightYellow' ANSI color in the terminal.
			["terminal.ansiCyan"] = term.cyan, -- 'Cyan' ANSI color in the terminal.
			["terminal.ansiGreen"] = term.green, -- 'Green' ANSI color in the terminal.
			["terminal.ansiMagenta"] = term.magenta, -- 'Magenta' ANSI color in the terminal.
			["terminal.ansiRed"] = term.red, -- 'Red' ANSI color in the terminal.
			["terminal.ansiWhite"] = term.white, -- 'White' ANSI color in the terminal.
			["terminal.ansiYellow"] = term.yellow, -- 'Yellow' ANSI color in the terminal.
			["terminalCursor.background"] = specs.TermCursor.bg, -- The background color of the terminal cursor. Allows customizing the color of a character overlapped by a block cursor.
			["terminalCursor.foreground"] = specs.TermCursor.fg, -- The foreground color of the terminal cursor.

			-- Bracket matches:

			-- Bracket colors
			-- ["editorBracketMatch.background"] = , -- Background color behind matching brackets.
			-- ["editorBracketMatch.border"] = , -- Color for matching brackets boxes.

			-- Bracket pair colorization:
			["editorBracketHighlight.foreground1"] = term.blue, -- Foreground color of brackets (1). Requires enabling bracket pair colorization.
			["editorBracketHighlight.foreground2"] = term.green, -- Foreground color of brackets (2). Requires enabling bracket pair colorization.
			["editorBracketHighlight.foreground3"] = term.magenta, -- Foreground color of brackets (3). Requires enabling bracket pair colorization.
			["editorBracketHighlight.foreground4"] = term.blue, -- Foreground color of brackets (4). Requires enabling bracket pair colorization.
			["editorBracketHighlight.foreground5"] = term.green, -- Foreground color of brackets (5). Requires enabling bracket pair colorization.
			["editorBracketHighlight.foreground6"] = term.magenta, -- Foreground color of brackets (6). Requires enabling bracket pair colorization.
			["editorBracketHighlight.unexpectedBracket.foreground"] = term.red, -- Foreground color of unexpected brackets.

			-- term.black
			-- term.blue
			-- term.cyan
			-- term.green
			-- term.magenta
			-- term.red
			-- term.white
			-- term.yellow

			-- Bracket pair guides:
			-- ["editorBracketPairGuide.activeBackground1"] = , -- Background color of active bracket pair guides (1). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.activeBackground2"] = , -- Background color of active bracket pair guides (2). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.activeBackground3"] = , -- Background color of active bracket pair guides (3). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.activeBackground4"] = , -- Background color of active bracket pair guides (4). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.activeBackground5"] = , -- Background color of active bracket pair guides (5). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.activeBackground6"] = , -- Background color of active bracket pair guides (6). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.background1"] = , -- Background color of inactive bracket pair guides (1). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.background2"] = , -- Background color of inactive bracket pair guides (2). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.background3"] = , -- Background color of inactive bracket pair guides (3). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.background4"] = , -- Background color of inactive bracket pair guides (4). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.background5"] = , -- Background color of inactive bracket pair guides (5). Requires enabling bracket pair guides.
			-- ["editorBracketPairGuide.background6"] = , -- Background color of inactive bracket pair guides (6). Requires enabling bracket pair guides.
		},
		["tokenColors"] = {
			-- {
			-- 	["name"] = "Comments",
			-- 	["scope"] = {
			-- 		"comment",
			-- 		"punctuation.definition.comment"
			-- 	},
			-- 	["settings"] = {
			-- 		["fontStyle"] = "italic",
			-- 		["foreground"] = "#AAAAAA"
			-- 	}
			-- },
			-- -- {
			-- -- 	["name"] = "Comments: Preprocessor",
			-- -- 	["scope"] = "comment.block.preprocessor",
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "",
			-- -- 		["foreground"] = "#AAAAAA"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Comments: Documentation",
			-- -- 	["scope"] = {
			-- -- 		"comment.documentation",
			-- -- 		"comment.block.documentation"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#448C27"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Invalid - Illegal",
			-- -- 	["scope"] = "invalid.illegal",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#660000"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Operators",
			-- -- 	["scope"] = "keyword.operator",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#777777"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Keywords",
			-- -- 	["scope"] = {
			-- -- 		"keyword",
			-- -- 		"storage"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#4B83CD"
			-- -- 	}
			-- -- },
			-- {
			-- 	["name"] = "Types",
			-- 	["scope"] = {
			-- 		"storage.type",
			-- 		"support.type"
			-- 	},
			-- 	["settings"] = {
			-- 		["foreground"] = specs.Type.fg
			-- 	}
			-- },
			-- -- {
			-- -- 	["name"] = "Language Constants",
			-- -- 	["scope"] = {
			-- -- 		"constant.language",
			-- -- 		"support.constant",
			-- -- 		"variable.language"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = specs.Constant.gui,
			-- -- 		["foreground"] = specs.Constant.fg,
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Variables",
			-- -- 	["scope"] = {
			-- -- 		"variable",
			-- -- 		"support.variable"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#7A3E9D"
			-- -- 	}
			-- -- },
			-- {
			-- 	["name"] = "Functions",
			-- 	["scope"] = {
			-- 		"entity.name.function",
			-- 		"support.function"
			-- 	},
			-- 	["settings"] = {
			-- 		["foreground"] = specs.Function.fg
			-- 	}
			-- },
			-- -- {
			-- -- 	["name"] = "Classes",
			-- -- 	["scope"] = {
			-- -- 		"entity.name.type",
			-- -- 		"entity.other.inherited-class",
			-- -- 		"support.class"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "bold",
			-- -- 		["foreground"] = "#7A3E9D"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Exceptions",
			-- -- 	["scope"] = "entity.name.exception",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#660000"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Sections",
			-- -- 	["scope"] = "entity.name.section",
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "bold"
			-- -- 	}
			-- -- },
			-- {
			-- 	["name"] = "Constant",
			-- 	["scope"] = {
			-- 		"constant"
			-- 	},
			-- 	["settings"] = {
			-- 		["foreground"] = specs.Constant.fg
			-- 	}
			-- },
			-- {
			-- 	["name"] = "Number",
			-- 	["scope"] = {
			-- 		"constant.numeric"
			-- 	},
			-- 	["settings"] = {
			-- 		["fontStyle"] = specs.Number.gui,
			-- 		["foreground"] = specs.Number.fg
			-- 	}
			-- },
			-- -- {
			-- -- 	["name"] = "Strings",
			-- -- 	["scope"] = "string",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#448C27"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Strings: Escape Sequences",
			-- -- 	["scope"] = "constant.character.escape",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#777777"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Strings: Regular Expressions",
			-- -- 	["scope"] = "string.regexp",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#4B83CD"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Strings: Symbols",
			-- -- 	["scope"] = "constant.other.symbol",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#AB6526"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Punctuation",
			-- -- 	["scope"] = "punctuation",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#777777"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "HTML: Doctype Declaration",
			-- -- 	["scope"] = {
			-- -- 		"meta.tag.sgml.doctype",
			-- -- 		"meta.tag.sgml.doctype string",
			-- -- 		"meta.tag.sgml.doctype entity.name.tag",
			-- -- 		"meta.tag.sgml punctuation.definition.tag.html"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#AAAAAA"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "HTML: Tags",
			-- -- 	["scope"] = {
			-- -- 		"meta.tag",
			-- -- 		"punctuation.definition.tag.html",
			-- -- 		"punctuation.definition.tag.begin.html",
			-- -- 		"punctuation.definition.tag.end.html"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#91B3E0"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "HTML: Tag Names",
			-- -- 	["scope"] = "entity.name.tag",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#4B83CD"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "HTML: Attribute Names",
			-- -- 	["scope"] = {
			-- -- 		"meta.tag entity.other.attribute-name",
			-- -- 		"entity.other.attribute-name.html"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "italic",
			-- -- 		["foreground"] = "#91B3E0"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "HTML: Entities",
			-- -- 	["scope"] = {
			-- -- 		"constant.character.entity",
			-- -- 		"punctuation.definition.entity"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#AB6526"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "CSS: Selectors",
			-- -- 	["scope"] = {
			-- -- 		"meta.selector",
			-- -- 		"meta.selector entity",
			-- -- 		"meta.selector entity punctuation",
			-- -- 		"entity.name.tag.css"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#7A3E9D"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "CSS: Property Names",
			-- -- 	["scope"] = {
			-- -- 		"meta.property-name",
			-- -- 		"support.type.property-name"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#AB6526"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "CSS: Property Values",
			-- -- 	["scope"] = {
			-- -- 		"meta.property-value",
			-- -- 		"meta.property-value constant.other",
			-- -- 		"support.constant.property-value"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#448C27"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "CSS: Important Keyword",
			-- -- 	["scope"] = "keyword.other.important",
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "bold"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Changed",
			-- -- 	["scope"] = "markup.changed",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#000000"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Deletion",
			-- -- 	["scope"] = "markup.deleted",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#000000"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Emphasis",
			-- -- 	["scope"] = "markup.italic",
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "italic"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Error",
			-- -- 	["scope"] = "markup.error",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#660000"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Insertion",
			-- -- 	["scope"] = "markup.inserted",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#000000"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Link",
			-- -- 	["scope"] = "meta.link",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#4B83CD"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Output",
			-- -- 	["scope"] = {
			-- -- 		"markup.output",
			-- -- 		"markup.raw"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#777777"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Prompt",
			-- -- 	["scope"] = "markup.prompt",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#777777"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Heading",
			-- -- 	["scope"] = "markup.heading",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#AA3731"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Strong",
			-- -- 	["scope"] = "markup.bold",
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "bold"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Traceback",
			-- -- 	["scope"] = "markup.traceback",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#660000"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup: Underline",
			-- -- 	["scope"] = "markup.underline",
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "underline"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup Quote",
			-- -- 	["scope"] = "markup.quote",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#7A3E9D"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup Lists",
			-- -- 	["scope"] = "markup.list",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#4B83CD"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup Styling",
			-- -- 	["scope"] = {
			-- -- 		"markup.bold",
			-- -- 		"markup.italic"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#448C27"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Markup Inline",
			-- -- 	["scope"] = "markup.inline.raw",
			-- -- 	["settings"] = {
			-- -- 		["fontStyle"] = "",
			-- -- 		["foreground"] = "#AB6526"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Extra: Diff Range",
			-- -- 	["scope"] = {
			-- -- 		"meta.diff.range",
			-- -- 		"meta.diff.index",
			-- -- 		"meta.separator"
			-- -- 	},
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#434343"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Extra: Diff From",
			-- -- 	["scope"] = "meta.diff.header.from-file",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#434343"
			-- -- 	}
			-- -- },
			-- -- {
			-- -- 	["name"] = "Extra: Diff To",
			-- -- 	["scope"] = "meta.diff.header.to-file",
			-- -- 	["settings"] = {
			-- -- 		["foreground"] = "#434343"
			-- -- 	}
			-- -- }


			{
				["name"] = "Comment",
				["scope"] = {
					"comment"
				},
				["settings"] = to_token_colors(specs.Comment),
			},
			{
				["name"] = "Constant",
				["scope"] = {
					"variable.other.constant",
					"constant",
					"variable.language",
					"support.constant"
				},
				["settings"] = to_token_colors(specs.Constant),
			},
			{
				["name"] = "String",
				["scope"] = {
					"string"
				},
				["settings"] = to_token_colors(specs.String),
			},
			{
				["name"] = "Number",
				["scope"] = {
					"constant.numeric"
				},
				["settings"] = to_token_colors(specs.Number),
			},
			{
				["name"] = "Boolean",
				["scope"] = {
					"constant.language.boolean"
				},
				["settings"] = to_token_colors(specs.Boolean),
			},
			{
				["name"] = "Identifier",
				["scope"] = {
					"variable.other",
					"support.variable"
				},
				["settings"] = to_token_colors(specs.Identifier),
			},
			{
				["name"] = "Function",
				["scope"] = {
					"entity.name.function",
					"support.function"
				},
				["settings"] = to_token_colors(specs.Function),
			},
			{
				["name"] = "Statement",
				["scope"] = {
					"keyword",
				},
				["settings"] = to_token_colors(specs.Statement),
			},
			{
				["name"] = "PreProc",
				["scope"] = {
					"keyword.control.directive",
				},
				["settings"] = to_token_colors(specs.PreProc),
			},
			{
				["name"] = "Type",
				["scope"] = {
					"storage.type",
					"storage.modifier",
					"keyword.other.typedef",
					"entity.name.type",
					"entity.other.inherited-class",
					"support.type",
					"support.class",
				},
				["settings"] = to_token_colors(specs.Type),
			},
			{
				["name"] = "Special",
				["scope"] = {
					"punctuation.definition.template-expression",
				},
				["settings"] = to_token_colors(specs.Special),
			},
			{
				["name"] = "Tag",
				["scope"] = {
					"entity.name.tag",
				},
				["settings"] = to_token_colors(specs.Special),
			},
			{
				["name"] = "PropertyName",
				["scope"] = {
					"support.type.property-name",
					"variable.other.property",
				},
				["settings"] = to_token_colors(specs.Identifier),
			},
		}
	}
end

local function to_json(el)
	if type(el) == "table" and el["hex"] == nil then
		local object = true
		for key, value in pairs(el) do
			if type(key) == "string" then
				object = true
			elseif type(key) == "number" then
				object = false
			end
			break
		end
		local properties = {}
		if object == true then
			for key, value in pairs(el) do
				table.insert(properties, "\"" .. key .. "\":" .. to_json(value))
			end
			return "{" .. table.concat(properties, ",") .. "}"
		else
			for key, value in ipairs(el) do
				table.insert(properties, to_json(value))
			end
			return "[" .. table.concat(properties, ",") .. "]"
		end
	elseif type(el) == "number" then
		return tostring(el)
	else
		return "\"" .. tostring(el) .. "\""
	end
end

local function to_vscode(colorscheme)
	local table = to_table(colorscheme)
	local json = to_json(table)
	return { json }
end

local ness_list = { "dim", "bright", "stark", "warm", "default" }

for _, ness in ipairs(ness_list) do
	local specs_name = ness ~= "default" and "specs_" .. ness or "specs"
	local specs = getfenv()[specs_name]

	local metadata = {
		Palette = name:gsub("_%a+", ""),
		Background = background,
		Ness = ness,
	}

	if specs then
		---@diagnostic disable: undefined-global
		-- selene: allow(undefined_variable)
		run(
			{ specs, term, metadata },
			to_vscode,
			-- { prepend, "// This file is auto-generated by shipwright.nvim" },
			{ overwrite, string.format("extras/vscode/themes/%s_%s.json", name, ness) }
		)
		-- selene: deny(undefined_variable)
		---@diagnostic enable: undefined-global
	end
end
