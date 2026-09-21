"""Standalone, read-only ChartSage stock-specific technical alert monitor."""

from __future__ import annotations

import copy
import html
import json
import math
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

# A source launch from VS Code uses whichever system interpreter is selected.
# Put the project's consolidated dependency set first so "Run Python File in
# Terminal" behaves exactly like the dedicated launcher and cannot pick up a
# partial user installation of openpyxl/PySide6.
_SOURCE_DIR = Path(__file__).resolve().parent
_SOURCE_RUNTIME = _SOURCE_DIR / "_packaging_runtime"
if not getattr(sys, "frozen", False) and _SOURCE_RUNTIME.is_dir():
    runtime_text = str(_SOURCE_RUNTIME)
    if not sys.path or sys.path[0] != runtime_text:
        sys.path.insert(0, runtime_text)

import ChartSageAlgoOPENcode as core
from stock_alert_engine import (
    StockAlertStore,
    TRIGGER_EVERY_MATCH,
    TRIGGER_ONCE,
    TRIGGER_TRANSITION,
    next_due_at,
    should_trigger,
)

from PySide6.QtCore import QDate, QThread, QTime, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
ALERTS_DB = APP_DIR / "stock_alerts.db"


TRIGGER_LABELS = {
    TRIGGER_TRANSITION: "When condition becomes true",
    TRIGGER_ONCE: "Once, then disable alert",
    TRIGGER_EVERY_MATCH: "Every matching evaluation",
}


def eligible_templates() -> dict[str, dict[str, Any]]:
    templates = core.TemplateStore(core.TEMPLATE_FILE).load_all()
    return {
        name: setup
        for name, setup in templates.items()
        if core.has_active_rule(setup.get("expression", {}))
        and not core.is_final_template_setup(name, setup)
        and not core.expression_has_pnl_rules(setup.get("expression"))
        and not core.setup_has_portfolio_exit_triggers(setup)
    }


def load_nse_symbols() -> list[tuple[str, str]]:
    try:
        payload = json.loads(core.NSE_SYMBOLS_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("symbols", []) if isinstance(payload, dict) else []
    result: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "") or "").strip().upper()
        name = str(row.get("name", "") or "").strip()
        if symbol:
            result.append((symbol, name))
    return sorted(set(result), key=lambda item: item[0])


def clean_error(details: str) -> str:
    lines = [line.strip() for line in str(details or "").splitlines() if line.strip()]
    return lines[-1] if lines else "Evaluation failed."


def alert_expression_text(alert: dict[str, Any]) -> str:
    expression = alert.get("setup", {}).get("expression")
    if not isinstance(expression, dict):
        return "Invalid alert rule"
    try:
        return core.expression_text(expression)
    except (KeyError, TypeError, ValueError):
        return "Invalid alert rule"


class LegacyStockAlertDialog(QDialog):
    def __init__(
        self,
        templates: dict[str, dict[str, Any]],
        symbols: list[tuple[str, str]],
        existing: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.existing = existing or {}
        self.templates = dict(templates)
        stored_template = str(self.existing.get("template_name", "") or "")
        if stored_template and isinstance(self.existing.get("setup"), dict):
            # Editing delivery/timing must not silently adopt later changes to
            # the saved template. Recreating the alert is the explicit refresh.
            self.templates[stored_template] = copy.deepcopy(self.existing["setup"])
        self.setWindowTitle("Edit Stock Alert" if existing else "Create Stock Alert")
        self.resize(760, 690)
        layout = QVBoxLayout(self)

        note = QLabel(
            "Choose one stock, then use either a direct price condition or a saved "
            "ChartSage technical rule template. A complete snapshot of the rule is "
            "stored with the alert. This monitor never places orders."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            "background:#e8f4fd; color:#0b3d66; border:1px solid #9bc9ea; padding:7px;"
        )
        layout.addWidget(note)

        form = QFormLayout()
        self.name_edit = QLineEdit(str(self.existing.get("name", "") or ""))
        self.name_edit.setPlaceholderText("Optional; generated automatically if blank")
        form.addRow("Alert name:", self.name_edit)

        self.symbol_combo = QComboBox()
        self.symbol_combo.setEditable(True)
        self.symbol_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for symbol, company in symbols:
            display = f"{symbol} — {company}" if company else symbol
            self.symbol_combo.addItem(display, symbol)
        self.symbol_combo.setCompleter(QCompleter(
            [self.symbol_combo.itemText(i) for i in range(self.symbol_combo.count())],
            self.symbol_combo,
        ))
        self.symbol_combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        existing_symbol = str(self.existing.get("symbol", "") or "").upper()
        if existing_symbol:
            index = self.symbol_combo.findData(existing_symbol)
            self.symbol_combo.setCurrentIndex(index if index >= 0 else -1)
            if index < 0:
                self.symbol_combo.setEditText(existing_symbol)
        else:
            self.symbol_combo.setCurrentIndex(-1)
        form.addRow("NSE stock:", self.symbol_combo)

        self.alert_type_combo = QComboBox()
        self.alert_type_combo.addItem("Saved ChartSage technical rule", "TEMPLATE")
        self.alert_type_combo.addItem("Direct price condition", "PRICE")
        is_price_alert = str(self.existing.get("template_name", "")).startswith("[PRICE]")
        if is_price_alert or not self.templates:
            self.alert_type_combo.setCurrentIndex(
                self.alert_type_combo.findData("PRICE")
            )
        form.addRow("Alert type:", self.alert_type_combo)

        self.template_combo = QComboBox()
        for name in sorted(self.templates, key=str.upper):
            self.template_combo.addItem(name, name)
        existing_template = str(self.existing.get("template_name", "") or "")
        if existing_template:
            index = self.template_combo.findData(existing_template)
            if (
                index < 0
                and not is_price_alert
                and isinstance(self.existing.get("setup"), dict)
            ):
                self.template_combo.addItem(existing_template + " (stored snapshot)", existing_template)
                index = self.template_combo.count() - 1
            self.template_combo.setCurrentIndex(max(0, index))
        form.addRow("Rule template:", self.template_combo)

        price_row = QHBoxLayout()
        self.price_operator_combo = QComboBox()
        for label, value in (
            ("Crosses above", "crosses above"),
            ("Crosses below", "crosses below"),
            ("Greater than", ">"),
            ("Less than", "<"),
            ("Greater than or equal to", ">="),
            ("Less than or equal to", "<="),
        ):
            self.price_operator_combo.addItem(label, value)
        self.price_value_spin = QDoubleSpinBox()
        self.price_value_spin.setRange(0.0001, 100_000_000.0)
        self.price_value_spin.setDecimals(4)
        self.price_value_spin.setValue(100.0)
        self.price_value_spin.setPrefix("₹ ")
        price_row.addWidget(self.price_operator_combo, 1)
        price_row.addWidget(self.price_value_spin, 1)
        form.addRow("Price condition:", price_row)
        if is_price_alert:
            expression = self.existing.get("setup", {}).get("expression", {})
            if expression.get("type") == "rule":
                operator = str(expression.get("operator", "crosses above"))
                index = self.price_operator_combo.findData(operator)
                self.price_operator_combo.setCurrentIndex(max(0, index))
                right = expression.get("right", {})
                if right.get("kind") == "value":
                    self.price_value_spin.setValue(float(right.get("value", 100.0)))

        self.interval_combo = QComboBox()
        for minutes in (1, 2, 5, 10, 15, 30, 60):
            self.interval_combo.addItem(f"Every {minutes} minute(s)", minutes)
        interval = int(self.existing.get("interval_minutes", 5) or 5)
        self.interval_combo.setCurrentIndex(max(0, self.interval_combo.findData(interval)))
        form.addRow("Evaluate:", self.interval_combo)

        self.trigger_combo = QComboBox()
        for value in (TRIGGER_TRANSITION, TRIGGER_ONCE, TRIGGER_EVERY_MATCH):
            self.trigger_combo.addItem(TRIGGER_LABELS[value], value)
        trigger_mode = str(self.existing.get("trigger_mode", TRIGGER_TRANSITION))
        self.trigger_combo.setCurrentIndex(max(0, self.trigger_combo.findData(trigger_mode)))
        form.addRow("Notify:", self.trigger_combo)

        self.include_current_cb = QCheckBox(
            "Include the current unfinished daily/weekly/monthly candle"
        )
        self.adjusted_cb = QCheckBox(
            "Use technically adjusted prices (splits/bonus/consolidation)"
        )
        self.telegram_cb = QCheckBox("Send Telegram notification when triggered")
        self.enabled_cb = QCheckBox("Alert active")
        self.include_current_cb.setChecked(bool(self.existing.get("include_current", True)))
        self.adjusted_cb.setChecked(bool(self.existing.get("adjusted", True)))
        self.telegram_cb.setChecked(bool(self.existing.get("telegram_enabled", True)))
        self.enabled_cb.setChecked(bool(self.existing.get("enabled", True)))
        form.addRow("Candle handling:", self.include_current_cb)
        form.addRow("Price basis:", self.adjusted_cb)
        form.addRow("Delivery:", self.telegram_cb)
        form.addRow("Status:", self.enabled_cb)

        expiry_row = QHBoxLayout()
        self.expiry_cb = QCheckBox("Expire after")
        self.expiry_date = QDateEdit(QDate.currentDate().addYears(1))
        self.expiry_date.setCalendarPopup(True)
        self.expiry_date.setDisplayFormat("dd-MM-yyyy")
        expires_at = str(self.existing.get("expires_at", "") or "")
        self.expiry_cb.setChecked(bool(expires_at))
        if expires_at:
            selected = QDate.fromString(expires_at[:10], Qt.DateFormat.ISODate)
            if selected.isValid():
                self.expiry_date.setDate(selected)
        self.expiry_date.setEnabled(self.expiry_cb.isChecked())
        self.expiry_cb.toggled.connect(self.expiry_date.setEnabled)
        expiry_row.addWidget(self.expiry_cb)
        expiry_row.addWidget(self.expiry_date)
        expiry_row.addStretch(1)
        form.addRow("Expiration:", expiry_row)
        layout.addLayout(form)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(180)
        self.preview.setPlaceholderText("Select a rule template to inspect its expression.")
        layout.addWidget(QLabel("Stored rule snapshot:"))
        layout.addWidget(self.preview, 1)
        self.template_combo.currentIndexChanged.connect(self._refresh_preview)
        self.alert_type_combo.currentIndexChanged.connect(self._refresh_alert_type)
        self.price_operator_combo.currentIndexChanged.connect(self._refresh_preview)
        self.price_value_spin.valueChanged.connect(self._refresh_preview)
        self._refresh_alert_type()
        self._refresh_preview()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _selected_symbol(self) -> str:
        text = self.symbol_combo.currentText().strip()
        data = self.symbol_combo.currentData()
        if data and text == self.symbol_combo.itemText(self.symbol_combo.currentIndex()):
            return str(data).strip().upper()
        return text.split("—", 1)[0].strip().upper()

    def _selected_setup(self) -> dict[str, Any] | None:
        if self.alert_type_combo.currentData() == "PRICE":
            setup = core.default_setup()
            setup["expression"] = core.rule_node(
                core.field_operand("Close", "D"),
                str(self.price_operator_combo.currentData() or "crosses above"),
                core.value_operand(float(self.price_value_spin.value())),
            )
            return setup
        name = str(self.template_combo.currentData() or "")
        return self.templates.get(name)

    def _refresh_alert_type(self, *_args: Any) -> None:
        price_mode = self.alert_type_combo.currentData() == "PRICE"
        self.template_combo.setEnabled(not price_mode)
        self.price_operator_combo.setEnabled(price_mode)
        self.price_value_spin.setEnabled(price_mode)
        self._refresh_preview()

    def _refresh_preview(self, *_args: Any) -> None:
        setup = self._selected_setup()
        if not setup:
            self.preview.clear()
            return
        try:
            text = core.expression_text(setup["expression"])
        except Exception:
            text = "The selected template expression could not be displayed."
        self.preview.setPlainText(text)

    def _accept(self) -> None:
        if not self._selected_symbol():
            QMessageBox.warning(self, "Stock required", "Select or enter an NSE stock symbol.")
            return
        if not self._selected_setup():
            QMessageBox.warning(self, "Rule required", "Select a saved rule template.")
            return
        self.accept()

    def value(self) -> dict[str, Any]:
        setup = self._selected_setup()
        if setup is None:
            raise ValueError("Select a valid rule.")
        if self.alert_type_combo.currentData() == "PRICE":
            operator = str(self.price_operator_combo.currentData())
            template_name = (
                f"[PRICE] Close {operator} {self.price_value_spin.value():.4f}"
            )
        else:
            template_name = str(self.template_combo.currentData() or "")
        expires_at = ""
        if self.expiry_cb.isChecked():
            expires_at = self.expiry_date.date().toString(Qt.DateFormat.ISODate) + "T23:59:59"
        return {
            "name": self.name_edit.text().strip(),
            "symbol": self._selected_symbol(),
            "template_name": template_name,
            "setup": copy.deepcopy(setup),
            "enabled": self.enabled_cb.isChecked(),
            "include_current": self.include_current_cb.isChecked(),
            "adjusted": self.adjusted_cb.isChecked(),
            "interval_minutes": int(self.interval_combo.currentData() or 5),
            "trigger_mode": str(self.trigger_combo.currentData() or TRIGGER_TRANSITION),
            "telegram_enabled": self.telegram_cb.isChecked(),
            "expires_at": expires_at,
        }


class AlertGroupLogicDialog(QDialog):
    """Edit one alert-local rule group's AND/OR/K-of-N logic."""

    def __init__(self, operator: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Group Logic")
        layout = QFormLayout(self)
        self.logic = QComboBox()
        self.logic.addItem("AND — every condition must match", "AND")
        self.logic.addItem("OR — any condition may match", "OR")
        self.logic.addItem("K-of-N — at least K conditions must match", "ANY")
        self.k = QSpinBox()
        self.k.setRange(1, 99)
        clean = str(operator or "AND")
        if clean.startswith("ANY "):
            self.logic.setCurrentIndex(self.logic.findData("ANY"))
            try:
                self.k.setValue(max(1, int(clean.split()[1])))
            except (IndexError, ValueError):
                self.k.setValue(1)
        else:
            self.logic.setCurrentIndex(max(0, self.logic.findData(clean)))
        self.logic.currentIndexChanged.connect(self._sync)
        layout.addRow("Combination:", self.logic)
        layout.addRow("K value:", self.k)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self._sync()

    def _sync(self, *_args: Any) -> None:
        self.k.setEnabled(self.logic.currentData() == "ANY")

    def operator(self) -> str:
        return (
            f"ANY {self.k.value()}"
            if self.logic.currentData() == "ANY"
            else str(self.logic.currentData() or "AND")
        )


class IndependentStockAlertDialog(QDialog):
    """TradingView-style, alert-local rule builder with optional template import."""

    def __init__(
        self,
        templates: dict[str, dict[str, Any]],
        symbols: list[tuple[str, str]],
        existing: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.templates = templates
        self.existing = existing or {}
        saved_expression = self.existing.get("setup", {}).get("expression")
        self.expression = copy.deepcopy(
            saved_expression
            if isinstance(saved_expression, dict)
            else core.group_node("AND", [])
        )
        if self.expression.get("type") != "group":
            self.expression = core.group_node("AND", [self.expression])
        self.rule_source = str(
            self.existing.get("template_name", "Custom alert rule")
            or "Custom alert rule"
        )
        self.item_paths: dict[tuple[int, ...], QTreeWidgetItem] = {}
        self.setWindowTitle("Edit Stock Alert" if existing else "Create Stock Alert")
        self.resize(1000, 720)
        self.setMinimumSize(760, 600)
        layout = QVBoxLayout(self)

        note = QLabel(
            "Build this stock's alert rule independently. Price and every supported "
            "indicator are available through Add condition. Saved ChartSage rules "
            "are optional imports only; editing here never changes them."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            "background:#e8f4fd; color:#0b3d66; border:1px solid #9bc9ea; padding:7px;"
        )
        layout.addWidget(note)

        form = QFormLayout()
        self.name_edit = QLineEdit(str(self.existing.get("name", "") or ""))
        self.name_edit.setPlaceholderText("Optional; generated automatically if blank")
        form.addRow("Alert name:", self.name_edit)
        self.symbol_combo = QComboBox()
        self.symbol_combo.setEditable(True)
        self.symbol_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for symbol, company in symbols:
            self.symbol_combo.addItem(
                f"{symbol} — {company}" if company else symbol, symbol
            )
        completer = QCompleter(
            [self.symbol_combo.itemText(i) for i in range(self.symbol_combo.count())],
            self.symbol_combo,
        )
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.symbol_combo.setCompleter(completer)
        existing_symbol = str(self.existing.get("symbol", "") or "").upper()
        if existing_symbol:
            index = self.symbol_combo.findData(existing_symbol)
            self.symbol_combo.setCurrentIndex(index if index >= 0 else -1)
            if index < 0:
                self.symbol_combo.setEditText(existing_symbol)
        else:
            self.symbol_combo.setCurrentIndex(-1)
        form.addRow("NSE stock:", self.symbol_combo)
        layout.addLayout(form)

        source_row = QHBoxLayout()
        self.source_label = QLabel()
        self.source_label.setStyleSheet("font-weight:600; color:#555;")
        source_row.addWidget(self.source_label, 1)
        import_button = QPushButton("Import saved ChartSage rule…")
        import_button.setEnabled(bool(self.templates))
        import_button.clicked.connect(self._import_template)
        blank_button = QPushButton("Start blank")
        blank_button.clicked.connect(self._start_blank)
        source_row.addWidget(import_button)
        source_row.addWidget(blank_button)
        layout.addLayout(source_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Alert-local rule expression"])
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.itemDoubleClicked.connect(lambda *_args: self._edit_selected())
        layout.addWidget(self.tree, 1)
        rule_buttons = QHBoxLayout()
        for label, handler in (
            ("Add condition…", self._add_condition),
            ("Add group", self._add_group),
            ("Edit selected…", self._edit_selected),
            ("Duplicate", self._duplicate_selected),
            ("Delete", self._delete_selected),
            ("Up", lambda: self._move_selected(-1)),
            ("Down", lambda: self._move_selected(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            rule_buttons.addWidget(button)
        rule_buttons.addStretch(1)
        layout.addLayout(rule_buttons)

        settings = QGroupBox("Monitoring and delivery")
        settings_form = QFormLayout(settings)
        self.interval_combo = QComboBox()
        for minutes in (1, 2, 5, 10, 15, 30, 60):
            self.interval_combo.addItem(f"Every {minutes} minute(s)", minutes)
        interval = int(self.existing.get("interval_minutes", 5) or 5)
        self.interval_combo.setCurrentIndex(max(0, self.interval_combo.findData(interval)))
        settings_form.addRow("Evaluate:", self.interval_combo)
        self.trigger_combo = QComboBox()
        for value in (TRIGGER_TRANSITION, TRIGGER_ONCE, TRIGGER_EVERY_MATCH):
            self.trigger_combo.addItem(TRIGGER_LABELS[value], value)
        trigger_mode = str(self.existing.get("trigger_mode", TRIGGER_TRANSITION))
        self.trigger_combo.setCurrentIndex(max(0, self.trigger_combo.findData(trigger_mode)))
        settings_form.addRow("Notify:", self.trigger_combo)
        self.include_current_cb = QCheckBox(
            "Include the current unfinished daily/weekly/monthly candle"
        )
        self.adjusted_cb = QCheckBox(
            "Use technically adjusted prices (splits/bonus/consolidation)"
        )
        self.telegram_cb = QCheckBox("Send Telegram notification when triggered")
        self.enabled_cb = QCheckBox("Alert active")
        self.include_current_cb.setChecked(bool(self.existing.get("include_current", True)))
        self.adjusted_cb.setChecked(bool(self.existing.get("adjusted", True)))
        self.telegram_cb.setChecked(bool(self.existing.get("telegram_enabled", True)))
        self.enabled_cb.setChecked(bool(self.existing.get("enabled", True)))
        settings_form.addRow("Candle handling:", self.include_current_cb)
        settings_form.addRow("Price basis:", self.adjusted_cb)
        settings_form.addRow("Delivery:", self.telegram_cb)
        settings_form.addRow("Status:", self.enabled_cb)
        expiry_row = QHBoxLayout()
        self.expiry_cb = QCheckBox("Expire after")
        self.expiry_date = QDateEdit(QDate.currentDate().addYears(1))
        self.expiry_date.setCalendarPopup(True)
        self.expiry_date.setDisplayFormat("dd-MM-yyyy")
        expires_at = str(self.existing.get("expires_at", "") or "")
        self.expiry_cb.setChecked(bool(expires_at))
        if expires_at:
            selected = QDate.fromString(expires_at[:10], Qt.DateFormat.ISODate)
            if selected.isValid():
                self.expiry_date.setDate(selected)
        self.expiry_date.setEnabled(self.expiry_cb.isChecked())
        self.expiry_cb.toggled.connect(self.expiry_date.setEnabled)
        expiry_row.addWidget(self.expiry_cb)
        expiry_row.addWidget(self.expiry_date)
        expiry_row.addStretch(1)
        settings_form.addRow("Expiration:", expiry_row)
        layout.addWidget(settings)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(100)
        layout.addWidget(QLabel("Complete alert expression:"))
        layout.addWidget(self.preview)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._rebuild_tree()

    def _selected_symbol(self) -> str:
        text = self.symbol_combo.currentText().strip()
        data = self.symbol_combo.currentData()
        if data and text == self.symbol_combo.itemText(self.symbol_combo.currentIndex()):
            return str(data).strip().upper()
        return text.split("—", 1)[0].strip().upper()

    def _selected_path(self) -> tuple[int, ...]:
        item = self.tree.currentItem()
        if item is None:
            return ()
        raw = item.data(0, Qt.ItemDataRole.UserRole)
        return tuple(raw or ())

    def _node(self, path: tuple[int, ...]) -> dict[str, Any]:
        node = self.expression
        for index in path:
            node = node["children"][index]
        return node

    def _target_group_path(self) -> tuple[int, ...]:
        path = self._selected_path()
        return path if self._node(path).get("type") == "group" else path[:-1]

    @staticmethod
    def _group_text(node: dict[str, Any]) -> str:
        operator = str(node.get("op", "AND"))
        if operator == "AND":
            return "AND — every enabled condition must match"
        if operator == "OR":
            return "OR — any enabled condition may match"
        return f"{operator} — K-of-N combination"

    def _rebuild_tree(self, selected: tuple[int, ...] | None = None) -> None:
        selected = self._selected_path() if selected is None and self.tree.topLevelItemCount() else (selected or ())
        self.tree.clear()
        self.item_paths = {}

        def add(node: dict[str, Any], path: tuple[int, ...], parent: QTreeWidgetItem | None) -> None:
            text = (
                self._group_text(node)
                if node.get("type") == "group"
                else core.expression_text(node)
            )
            item = QTreeWidgetItem([text])
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            self.item_paths[path] = item
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            if node.get("type") == "group":
                item.setFont(0, self.tree.font())
                for index, child in enumerate(node.get("children", [])):
                    add(child, path + (index,), item)

        add(self.expression, (), None)
        self.tree.expandAll()
        self.tree.setCurrentItem(self.item_paths.get(selected, self.item_paths[()]))
        self.source_label.setText(f"Rule source: {self.rule_source}")
        self.preview.setPlainText(
            core.expression_text(self.expression)
            if self.expression.get("children")
            else "No conditions yet. Click Add condition."
        )

    def _add_condition(self) -> None:
        dialog = core.RuleDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path = self._target_group_path()
        group = self._node(path)
        group.setdefault("children", []).append(dialog.result_rule())
        self.rule_source = "Custom alert rule"
        self._rebuild_tree(path + (len(group["children"]) - 1,))

    def _add_group(self) -> None:
        logic = AlertGroupLogicDialog("AND", self)
        if logic.exec() != QDialog.DialogCode.Accepted:
            return
        path = self._target_group_path()
        group = self._node(path)
        group.setdefault("children", []).append(core.group_node(logic.operator(), []))
        self.rule_source = "Custom alert rule"
        self._rebuild_tree(path + (len(group["children"]) - 1,))

    def _edit_selected(self) -> None:
        path = self._selected_path()
        node = self._node(path)
        if node.get("type") == "group":
            dialog = AlertGroupLogicDialog(str(node.get("op", "AND")), self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                node["op"] = dialog.operator()
                self.rule_source = "Custom alert rule"
                self._rebuild_tree(path)
            return
        dialog = core.RuleDialog(node, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            parent = self._node(path[:-1])
            parent["children"][path[-1]] = dialog.result_rule()
            self.rule_source = "Custom alert rule"
            self._rebuild_tree(path)

    def _duplicate_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        parent = self._node(path[:-1])
        parent["children"].insert(path[-1] + 1, copy.deepcopy(self._node(path)))
        self.rule_source = "Custom alert rule"
        self._rebuild_tree(path[:-1] + (path[-1] + 1,))

    def _delete_selected(self) -> None:
        path = self._selected_path()
        if not path:
            QMessageBox.information(self, "Root group", "The root group cannot be deleted.")
            return
        parent = self._node(path[:-1])
        del parent["children"][path[-1]]
        self.rule_source = "Custom alert rule"
        self._rebuild_tree(path[:-1])

    def _move_selected(self, delta: int) -> None:
        path = self._selected_path()
        if not path:
            return
        parent = self._node(path[:-1])
        target = path[-1] + int(delta)
        if not 0 <= target < len(parent["children"]):
            return
        parent["children"][path[-1]], parent["children"][target] = (
            parent["children"][target], parent["children"][path[-1]]
        )
        self.rule_source = "Custom alert rule"
        self._rebuild_tree(path[:-1] + (target,))

    def _import_template(self) -> None:
        names = sorted(self.templates, key=str.upper)
        if not names:
            return
        name, accepted = QInputDialog.getItem(
            self, "Import saved rule", "ChartSage rule template:", names, 0, False
        )
        if not accepted or not name:
            return
        expression = self.templates[str(name)].get("expression")
        if not isinstance(expression, dict):
            QMessageBox.warning(self, "Cannot import rule", "The selected template has no valid expression.")
            return
        self.expression = copy.deepcopy(expression)
        if self.expression.get("type") != "group":
            self.expression = core.group_node("AND", [self.expression])
        self.rule_source = f"Imported from {name}; alert-local copy"
        self._rebuild_tree(())

    def _start_blank(self) -> None:
        if self.expression.get("children") and QMessageBox.question(
            self,
            "Start with a blank rule",
            "Discard every condition currently shown in this alert editor?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.expression = core.group_node("AND", [])
        self.rule_source = "Custom alert rule"
        self._rebuild_tree(())

    def _accept(self) -> None:
        if not self._selected_symbol():
            QMessageBox.warning(self, "Stock required", "Select or enter an NSE stock symbol.")
            return
        if core.expression_has_pnl_rules(self.expression):
            QMessageBox.warning(
                self,
                "Unsupported alert condition",
                "Portfolio P&L conditions cannot be used for a single-stock alert. "
                "Use price or technical-indicator conditions instead.",
            )
            return
        try:
            core.validate_ast(self.expression)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid alert rule", str(exc))
            return
        self.accept()

    def value(self) -> dict[str, Any]:
        setup = core.default_setup()
        setup["expression"] = copy.deepcopy(self.expression)
        expires_at = ""
        if self.expiry_cb.isChecked():
            expires_at = self.expiry_date.date().toString(Qt.DateFormat.ISODate) + "T23:59:59"
        return {
            "name": self.name_edit.text().strip(),
            "symbol": self._selected_symbol(),
            "template_name": self.rule_source,
            "setup": setup,
            "enabled": self.enabled_cb.isChecked(),
            "include_current": self.include_current_cb.isChecked(),
            "adjusted": self.adjusted_cb.isChecked(),
            "interval_minutes": int(self.interval_combo.currentData() or 5),
            "trigger_mode": str(self.trigger_combo.currentData() or TRIGGER_TRANSITION),
            "telegram_enabled": self.telegram_cb.isChecked(),
            "expires_at": expires_at,
        }


class TelegramAlertWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def run(self) -> None:
        try:
            token, destinations = core.TelegramSettingsStore().load()
            enabled = [
                item for item in destinations
                if item.get("enabled") and str(item.get("chat_id", "")).strip()
            ]
            if not token or not enabled:
                raise core.TelegramSettingsError(
                    "Telegram is not configured with an enabled destination."
                )
            for destination in enabled:
                core.send_telegram_message(token, str(destination["chat_id"]), self.message)
            self.succeeded.emit(f"Telegram sent to {len(enabled)} destination(s).")
        except Exception as exc:
            self.failed.emit(str(exc))


class EventHistoryDialog(QDialog):
    def __init__(self, events: list[dict[str, Any]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Stock Alert History")
        self.resize(1100, 650)
        layout = QVBoxLayout(self)
        table = QTableWidget(len(events), 8)
        table.setHorizontalHeaderLabels([
            "Time", "Type", "Symbol", "Alert", "Matched", "Close", "Evidence", "Message"
        ])
        for row_index, event in enumerate(events):
            values = [
                event.get("occurred_at", ""), event.get("event_type", ""),
                event.get("symbol", ""), event.get("alert_name", ""),
                "" if event.get("matched") is None else "Yes" if event.get("matched") else "No",
                event.get("close_value", ""), event.get("evidence", ""), event.get("message", ""),
            ]
            for column, value in enumerate(values):
                table.setItem(row_index, column, QTableWidgetItem(str(value or "")))
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class StockAlertWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = StockAlertStore(ALERTS_DB)
        self.templates: dict[str, dict[str, Any]] = {}
        self.symbols = load_nse_symbols()
        self.worker: core.RuleScannerWorker | None = None
        self.current_alert_id: int | None = None
        self.monitoring = False
        self.force_queue: list[int] = []
        self.telegram_workers: set[TelegramAlertWorker] = set()
        self.setWindowTitle(
            f"ChartSage Stock Alerts — {core.session_price_mode_label()}"
        )
        self.resize(1450, 820)
        self._build_ui()
        self._load_templates()
        self._load_settings()
        self._refresh_table()
        self.timer = QTimer(self)
        self.timer.setInterval(5_000)
        self.timer.timeout.connect(self._scheduler_tick)
        self.timer.start()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        heading = QLabel("Stock-Specific Technical Alerts")
        heading.setStyleSheet("font-size:18px; font-weight:700; color:#17365d;")
        layout.addWidget(heading)
        description = QLabel(
            "Each alert monitors one NSE stock against its own price/technical rule. "
            "Build from scratch or optionally import and then edit a ChartSage rule. "
            "Guidance and notifications only—no broker orders."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        controls = QGroupBox("Monitoring controls")
        controls_layout = QHBoxLayout(controls)
        self.start_button = QPushButton("▶ Start Monitoring")
        self.start_button.setStyleSheet(
            "font-weight:700; background:#2e7d32; color:white; padding:7px 16px;"
        )
        self.start_button.clicked.connect(self._start_monitoring)
        self.stop_button = QPushButton("Stop Monitoring")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_monitoring)
        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addSpacing(20)
        controls_layout.addWidget(QLabel("Operating window:"))
        self.start_time = QTimeEdit(QTime(9, 15))
        self.start_time.setDisplayFormat("HH:mm")
        self.end_time = QTimeEdit(QTime(15, 30))
        self.end_time.setDisplayFormat("HH:mm")
        self.trading_days_cb = QCheckBox("Trading days only")
        self.trading_days_cb.setChecked(True)
        controls_layout.addWidget(self.start_time)
        controls_layout.addWidget(QLabel("to"))
        controls_layout.addWidget(self.end_time)
        controls_layout.addWidget(self.trading_days_cb)
        controls_layout.addStretch(1)
        self.mode_label = QLabel(core.session_price_mode_label())
        self.mode_label.setStyleSheet("font-weight:600; color:#0b5394;")
        controls_layout.addWidget(self.mode_label)
        layout.addWidget(controls)

        actions = QHBoxLayout()
        for label, handler in (
            ("New Alert…", self._new_alert),
            ("Edit Alert…", self._edit_alert),
            ("Delete Alert", self._delete_alert),
            ("Enable / Pause", self._toggle_alert),
            ("Evaluate Selected Now", self._evaluate_selected),
            ("Alert History…", self._show_history),
            ("↻ Reload Rule Templates", self._load_templates),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 14)
        self.table.setHorizontalHeaderLabels([
            "Active", "Symbol", "Alert name", "Alert rule", "Candle",
            "Interval", "Notify policy", "Condition", "Last evaluated",
            "Last triggered", "Triggers", "Last close", "Telegram", "Last error",
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._edit_alert)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(13, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Monitoring stopped")
        layout.addWidget(self.progress)
        self.status = QLabel("Ready. Create an alert, then start monitoring.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _load_templates(self) -> None:
        try:
            self.templates = eligible_templates()
            if hasattr(self, "status"):
                self.status.setText(
                    f"Loaded {len(self.templates):,} optional importable ChartSage rule(s)."
                )
        except Exception as exc:
            self.templates = {}
            if hasattr(self, "status"):
                self.status.setText(f"Could not load rule templates: {exc}")

    def _load_settings(self) -> None:
        self.start_time.setTime(QTime.fromString(
            str(self.store.get_setting("start_time", "09:15")), "HH:mm"
        ))
        self.end_time.setTime(QTime.fromString(
            str(self.store.get_setting("end_time", "15:30")), "HH:mm"
        ))
        self.trading_days_cb.setChecked(
            bool(self.store.get_setting("trading_days_only", True))
        )

    def _save_settings(self) -> None:
        self.store.set_setting("start_time", self.start_time.time().toString("HH:mm"))
        self.store.set_setting("end_time", self.end_time.time().toString("HH:mm"))
        self.store.set_setting("trading_days_only", self.trading_days_cb.isChecked())

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return int(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _require_selected(self) -> int | None:
        alert_id = self._selected_id()
        if alert_id is None:
            QMessageBox.information(self, "Select an alert", "Select an alert row first.")
        return alert_id

    def _refresh_table(self) -> None:
        selected_id = self._selected_id()
        alerts = self.store.list_alerts()
        self.table.setRowCount(len(alerts))
        selected_row = -1
        for row_index, alert in enumerate(alerts):
            condition = alert.get("last_condition")
            condition_text = "Not evaluated" if condition is None else "MATCHED" if condition else "Not matched"
            values = [
                "Yes" if alert["enabled"] else "Paused",
                alert["symbol"], alert["name"], alert_expression_text(alert),
                "Current included" if alert["include_current"] else "Completed candles only",
                f"{alert['interval_minutes']} min", TRIGGER_LABELS.get(alert["trigger_mode"], alert["trigger_mode"]),
                condition_text, alert["last_evaluated_at"], alert["last_triggered_at"],
                alert["trigger_count"], "" if alert["last_close"] is None else alert["last_close"],
                "On" if alert["telegram_enabled"] else "Off", alert["last_error"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(alert["id"]))
                if column == 3:
                    item.setToolTip(f"Source: {alert['template_name']}")
                if column == 7 and condition is True:
                    item.setBackground(Qt.GlobalColor.green)
                if column == 13 and alert["last_error"]:
                    item.setBackground(Qt.GlobalColor.red)
                self.table.setItem(row_index, column, item)
            if selected_id == int(alert["id"]):
                selected_row = row_index
        if selected_row >= 0:
            self.table.selectRow(selected_row)

    def _new_alert(self) -> None:
        dialog = IndependentStockAlertDialog(self.templates, self.symbols, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            alert_id = self.store.create_alert(dialog.value())
        except Exception as exc:
            QMessageBox.warning(self, "Alert not created", str(exc))
            return
        self._refresh_table()
        self.status.setText(f"Created stock alert #{alert_id}.")

    def _edit_alert(self, *_args: Any) -> None:
        alert_id = self._require_selected()
        if alert_id is None:
            return
        alert = self.store.get_alert(alert_id)
        if not alert:
            self._refresh_table()
            return
        dialog = IndependentStockAlertDialog(self.templates, self.symbols, alert, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.store.update_alert(alert_id, dialog.value())
        except Exception as exc:
            QMessageBox.warning(self, "Alert not updated", str(exc))
            return
        self._refresh_table()
        self.status.setText(f"Updated stock alert #{alert_id}; its trigger baseline was reset.")

    def _delete_alert(self) -> None:
        alert_id = self._require_selected()
        if alert_id is None:
            return
        alert = self.store.get_alert(alert_id)
        if not alert:
            return
        if QMessageBox.question(
            self, "Delete stock alert",
            f"Delete '{alert['name']}' and its event history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_alert(alert_id)
        self._refresh_table()
        self.status.setText("Alert deleted.")

    def _toggle_alert(self) -> None:
        alert_id = self._require_selected()
        if alert_id is None:
            return
        alert = self.store.get_alert(alert_id)
        if alert:
            self.store.set_enabled(alert_id, not bool(alert["enabled"]))
            self._refresh_table()

    def _evaluate_selected(self) -> None:
        alert_id = self._require_selected()
        if alert_id is None:
            return
        if alert_id not in self.force_queue:
            self.force_queue.append(alert_id)
        self._scheduler_tick()

    def _show_history(self) -> None:
        EventHistoryDialog(self.store.list_events(), self).exec()

    def _start_monitoring(self) -> None:
        if not self.store.list_alerts(enabled_only=True):
            QMessageBox.information(self, "No active alerts", "Create or enable at least one alert first.")
            return
        self._save_settings()
        self.monitoring = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.progress.setFormat("Monitoring active; looking for due alerts…")
        self.status.setText("Monitoring started.")
        self._scheduler_tick()

    def _stop_monitoring(self) -> None:
        self.monitoring = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.setFormat(
            "Monitoring stopped; current evaluation will finish."
            if self.worker and self.worker.isRunning() else "Monitoring stopped"
        )
        self.status.setText("Monitoring stopped.")

    def _within_window(self) -> bool:
        current = core.get_ist_now()
        if self.trading_days_cb.isChecked():
            try:
                if not core.NSE_CALENDAR.is_session_date(current.date()):
                    return False
            except Exception:
                if current.weekday() >= 5:
                    return False
        return self.start_time.time().toPython() <= current.time() <= self.end_time.time().toPython()

    def _scheduler_tick(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.store.expire_due_alerts()
        alert: dict[str, Any] | None = None
        while self.force_queue and alert is None:
            alert = self.store.get_alert(self.force_queue.pop(0))
        if alert is None:
            if not self.monitoring:
                return
            if not self._within_window():
                self.progress.setValue(0)
                self.progress.setFormat("Monitoring active; outside the selected operating window")
                return
            current = datetime.now().astimezone()
            due = [
                item for item in self.store.list_alerts(enabled_only=True)
                if next_due_at(item) <= current
            ]
            if due:
                due.sort(key=next_due_at)
                alert = due[0]
        if alert is not None:
            self._start_evaluation(alert)

    def _start_evaluation(self, alert: dict[str, Any]) -> None:
        setup = copy.deepcopy(alert.get("setup", {}))
        setup.update({
            "as_of_mode": "latest",
            "as_of_date": date.today().isoformat(),
            "adjusted": bool(alert["adjusted"]),
            "include_current": bool(alert["include_current"]),
            "refresh_minutes": int(alert["interval_minutes"]),
            "force_fresh": False,
            "auto_run": False,
            "telegram_alerts_enabled": False,
            "algo_auto_order_enabled": False,
            "algo_auto_order_cb": False,
            "algo_paper_mode": False,
        })
        self.current_alert_id = int(alert["id"])
        self.progress.setValue(1)
        self.progress.setFormat(
            f"%p% — Evaluating {alert['symbol']} — {alert['template_name']}"
        )
        self.worker = core.RuleScannerWorker(
            setup,
            symbols=[str(alert["symbol"])],
            symbol_details={str(alert["symbol"]): {"Universe": "Stock Alert"}},
            cache_path=core.PRICE_CACHE_DB,
        )
        self.worker.progress.connect(self._evaluation_progress)
        self.worker.succeeded.connect(self._evaluation_succeeded)
        self.worker.failed.connect(self._evaluation_failed)
        self.worker.cancelled.connect(
            lambda: self._evaluation_failed("Evaluation cancelled.")
        )
        self.worker.finished.connect(self._evaluation_finished)
        self.worker.start()

    def _evaluation_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(percent)
        self.progress.setFormat(f"%p% — {message}")

    def _evaluation_succeeded(self, report: core.ScanReport) -> None:
        if self.current_alert_id is None:
            return
        alert = self.store.get_alert(self.current_alert_id)
        if not alert:
            return
        if report.scanned_symbols < 1 or report.download_failure_symbols:
            self.store.record_error(
                int(alert["id"]),
                "No usable current price history was available for this stock.",
            )
            self.status.setText(f"{alert['symbol']}: evaluation had no usable price history.")
            return
        if report.insufficient_symbols and not report.rows:
            self.store.record_error(
                int(alert["id"]),
                "The alert rule did not have enough price history to calculate all operands.",
            )
            self.status.setText(f"{alert['symbol']}: insufficient history for the alert rule.")
            return
        matched = bool(report.rows)
        row = report.rows[0] if report.rows else {}
        close_value = row.get("Close")
        try:
            close_value = float(close_value) if close_value is not None and math.isfinite(float(close_value)) else None
        except (TypeError, ValueError):
            close_value = None
        evidence = str(row.get("Evidence", "") or "")
        triggered = should_trigger(
            str(alert["trigger_mode"]), matched, alert.get("last_condition")
        )
        disable = bool(triggered and alert["trigger_mode"] == TRIGGER_ONCE)
        self.store.record_evaluation(
            int(alert["id"]), matched, triggered=triggered,
            close_value=close_value, evidence=evidence, disable=disable,
        )
        if triggered:
            message = (
                "CHARTSAGE STOCK ALERT\n"
                f"{alert['symbol']} — {alert['name']}\n"
                f"Rule: {alert_expression_text(alert)}\n"
                f"Close: {close_value if close_value is not None else 'Unavailable'}\n"
                f"Candle: {'Current unfinished included' if alert['include_current'] else 'Completed candles only'}\n"
                f"Time: {core.get_ist_now():%Y-%m-%d %H:%M:%S} IST"
            )
            if evidence:
                message += "\nEvidence: " + evidence
            self.status.setText(f"TRIGGERED: {alert['symbol']} — {alert['name']}")
            if alert["telegram_enabled"]:
                self._send_telegram(message)
        else:
            self.status.setText(
                f"{alert['symbol']}: " + ("condition remains matched." if matched else "condition not matched.")
            )
        self._refresh_table()

    def _evaluation_failed(self, details: str) -> None:
        if self.current_alert_id is not None:
            self.store.record_error(self.current_alert_id, clean_error(details))
        self.status.setText("Evaluation failed: " + clean_error(details))
        self._refresh_table()

    def _evaluation_finished(self) -> None:
        self.progress.setValue(100)
        self.progress.setFormat("100% — Evaluation complete")
        self.worker = None
        self.current_alert_id = None
        QTimer.singleShot(0, self._scheduler_tick)

    def _send_telegram(self, message: str) -> None:
        worker = TelegramAlertWorker(message)
        self.telegram_workers.add(worker)
        worker.succeeded.connect(
            lambda result: self.status.setText(self.status.text() + " " + result)
        )
        worker.failed.connect(
            lambda error: self.status.setText(
                self.status.text() + " Telegram failed: " + error
            )
        )
        worker.finished.connect(lambda: self.telegram_workers.discard(worker))
        worker.start()

    def closeEvent(self, event: Any) -> None:
        self._save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5_000)
        for worker in list(self.telegram_workers):
            if worker.isRunning():
                worker.wait(5_000)
        event.accept()


def main() -> int:
    if "--portable-smoke-test" in sys.argv:
        import shiboken6  # noqa: F401
        app = QApplication.instance() or QApplication([])
        store = StockAlertStore(ALERTS_DB)
        if store.quick_check() != "ok":
            return 3
        app.quit()
        return 0
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    if not core.choose_startup_price_mode():
        return 0
    maintenance_report = core.run_startup_maintenance_dialog()
    window = StockAlertWindow()
    window.setWindowTitle(
        f"ChartSage Stock Alerts — {core.session_price_mode_label()}"
    )
    window.showMaximized()
    QTimer.singleShot(
        0,
        lambda: core.show_startup_maintenance_summary(
            window, maintenance_report
        ),
    )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
