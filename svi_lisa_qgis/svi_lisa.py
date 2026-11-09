# -*- coding: utf-8 -*-
"""SVI and LISA experimental QGIS plugin."""
import csv
from datetime import datetime
import math
import os
import random
from collections import Counter

from qgis.PyQt.QtCore import QCoreApplication, QEventLoop
from qgis.PyQt.QtWidgets import (QAction, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget)
from qgis.core import (QgsFeature, QgsField, QgsGeometry, QgsGraduatedSymbolRenderer, QgsRendererRange, QgsRendererCategory, QgsCategorizedSymbolRenderer, QgsClassificationJenks,
    QgsProject, QgsSpatialIndex, QgsSymbol, QgsVectorFileWriter, QgsVectorLayer,
    QgsWkbTypes)
from qgis.PyQt.QtGui import QColor, QIcon
try:
    from qgis.PyQt.QtCore import QMetaType
except ImportError:  # QGIS releases based on older Qt versions
    QMetaType = None
try:
    from qgis.PyQt.QtCore import QVariant
except ImportError:  # QVariant is represented by native Python types in some Qt6 builds
    QVariant = None
from qgis.core import Qgis


def _field_type(name):
    """Return a QgsField type accepted by both Qt5/QGIS 3 and Qt6/QGIS 4."""
    if QMetaType is not None:
        try:
            meta_type = getattr(QMetaType.Type, name)
            # QGIS 3.38+ and QGIS 4 accept QMetaType.Type in QgsField.
            QgsField("_type_check", meta_type)
            return meta_type
        except (AttributeError, TypeError):
            pass
    if QVariant is not None:
        # PyQt5 calls the QVariant string enum "String"; Qt6 uses "QString".
        legacy_name = {"QString": "String"}.get(name, name)
        if hasattr(QVariant, legacy_name):
            return getattr(QVariant, legacy_name)
    raise RuntimeError("Unable to determine the QGIS field type: " + name)


def _polygon_geometry_type():
    """Handle scoped geometry enums introduced for newer QGIS APIs."""
    if getattr(Qgis, "QGIS_VERSION_INT", 0) >= 40000:
        return Qgis.GeometryType.Polygon
    return QgsWkbTypes.PolygonGeometry


class SviLisaPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        self.action = QAction(QIcon(icon_path), "G-CHAM SVI and LISA", self.iface.mainWindow())
        self.action.setIconVisibleInMenu(True)
        self.action.setToolTip("G-CHAM SVI and LISA")
        self.action.setStatusTip("Calculate an SVI and run local Moran’s I (LISA).")
        self.action.triggered.connect(self.run)
        self.iface.pluginMenu().addAction(self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action:
            self.iface.pluginMenu().removeAction(self.action)
            self.iface.removeToolBarIcon(self.action)

    def run(self):
        if not self.dialog:
            self.dialog = MainDialog(self.iface)
        self.dialog.refresh_layers()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()


class MainDialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("G-CHAM SVI and LISA")
        self.resize(720, 650)
        self._output_session_active = False
        self.log_dialog = None
        self.log_output = None
        self._log_close_button = None
        self._log_buttons = []
        root = QVBoxLayout(self)
        note = QLabel("Calculate an SVI from a polygon layer or run a standalone LISA analysis.")
        note.setWordWrap(True)
        root.addWidget(note)
        form = QFormLayout()
        self.layer_combo = QComboBox()
        form.addRow("Input polygon layer", self.layer_combo)
        root.addLayout(form)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self._build_svi_tab()
        self._build_lisa_tab()
        # Qt6 scopes this enum under StandardButton; Qt5 exposed it directly.
        close_button = getattr(getattr(QDialogButtonBox, "StandardButton", QDialogButtonBox), "Close")
        buttons = QDialogButtonBox(close_button)
        buttons.rejected.connect(self.close)
        root.addWidget(buttons)
        self.layer_combo.currentIndexChanged.connect(self.refresh_fields)
        self.refresh_layers()

    def _build_svi_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("For each indicator, select a raw value or a numerator and denominator. Check Reverse when higher values indicate lower vulnerability."))
        self.svi_table = QTableWidget(0, 6)
        self.svi_table.setHorizontalHeaderLabels(["Indicator name", "Calculation", "Raw value", "Numerator", "Denominator", "Reverse"])
        self.svi_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.svi_table)
        row = QHBoxLayout()
        add = QPushButton("Add indicator"); add.clicked.connect(self.add_indicator)
        remove = QPushButton("Remove selected rows"); remove.clicked.connect(self.remove_indicator)
        row.addWidget(add); row.addWidget(remove); row.addStretch(1)
        layout.addLayout(row)
        self.add_indicator()
        self.svi_path = QLabel("Output: temporary layer")
        path_btn = QPushButton("Choose output"); path_btn.clicked.connect(self.choose_output)
        self.output_path = None
        outrow = QHBoxLayout(); outrow.addWidget(self.svi_path); outrow.addWidget(path_btn)
        layout.addLayout(outrow)
        self.auto_lisa = QCheckBox("Run LISA on svi_sum after calculating the SVI")
        self.auto_lisa.setChecked(True)
        layout.addWidget(self.auto_lisa)
        calc = QPushButton("Calculate SVI and add layer")
        calc.clicked.connect(self.calculate_svi)
        self._log_buttons.append(calc)
        layout.addWidget(calc)
        self.tabs.addTab(tab, "Calculate SVI")

    def _build_lisa_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.lisa_field = QComboBox(); form.addRow("Numeric field to analyze", self.lisa_field)
        self.k_spin = QSpinBox(); self.k_spin.setRange(1, 100); self.k_spin.setValue(8)
        form.addRow("Number of neighbors k (nearest neighbors)", self.k_spin)
        self.perm_spin = QSpinBox(); self.perm_spin.setRange(99, 9999); self.perm_spin.setSingleStep(100); self.perm_spin.setValue(999)
        form.addRow("Number of permutations", self.perm_spin)
        self.alpha_spin = QDoubleSpinBox(); self.alpha_spin.setRange(0.001, 0.2); self.alpha_spin.setDecimals(3); self.alpha_spin.setSingleStep(0.01); self.alpha_spin.setValue(0.05)
        form.addRow("Significance level (after FDR correction)", self.alpha_spin)
        layout.addLayout(form)
        self.fdr_check = QCheckBox("Use Benjamini–Hochberg FDR correction")
        self.fdr_check.setChecked(True); layout.addWidget(self.fdr_check)
        self.lisa_path = QLabel("Output: temporary layer")
        lisa_path_btn = QPushButton("Choose output"); lisa_path_btn.clicked.connect(self.choose_output)
        row = QHBoxLayout(); row.addWidget(self.lisa_path); row.addWidget(lisa_path_btn); layout.addLayout(row)
        explain = QLabel("Uses k-nearest neighbors with row-standardized weights. The result includes local Moran’s I, permutation p values, FDR-adjusted p values, and cluster categories. Results are saved to the selected GeoPackage. When LISA runs automatically after SVI, it is saved as a separate layer in the same file. Large datasets may take a long time to process.")
        explain.setWordWrap(True); layout.addWidget(explain)
        calc = QPushButton("Calculate LISA and add layer")
        calc.clicked.connect(self.calculate_lisa)
        self._log_buttons.append(calc)
        layout.addWidget(calc); layout.addStretch(1)
        self.tabs.addTab(tab, "Run LISA only")

    def _start_log(self, title):
        if self.log_dialog is None:
            self.log_dialog = QDialog(self)
            self.log_dialog.setWindowTitle("G-CHAM SVI and LISA — Processing log")
            self.log_dialog.resize(600, 340)
            layout = QVBoxLayout(self.log_dialog)
            self.log_output = QPlainTextEdit(self.log_dialog)
            self.log_output.setReadOnly(True)
            self.log_output.document().setMaximumBlockCount(500)
            layout.addWidget(self.log_output)
            self._log_close_button = QPushButton("Close", self.log_dialog)
            self._log_close_button.clicked.connect(self.log_dialog.hide)
            layout.addWidget(self._log_close_button)
        self.log_output.clear()
        self.log_dialog.setWindowTitle("G-CHAM SVI and LISA — " + title + " log")
        self.log_dialog.show()
        self.log_dialog.raise_()
        self.log_dialog.activateWindow()
        self._log_close_button.setEnabled(False)
        for button in self._log_buttons:
            button.setEnabled(False)
        self._append_log(title + " started.")

    def _append_log(self, message):
        if self.log_output is None:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.appendPlainText(f"[{timestamp}] {message}")
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        try:
            flags = QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        except AttributeError:
            flags = QEventLoop.ExcludeUserInputEvents
        QCoreApplication.processEvents(flags)

    def _finish_log(self, message):
        self._append_log(message)
        self._log_close_button.setEnabled(True)
        for button in self._log_buttons:
            button.setEnabled(True)

    def refresh_layers(self):
        current = self.layer_combo.currentData()
        self.layer_combo.blockSignals(True); self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.isValid() and QgsWkbTypes.geometryType(layer.wkbType()) == _polygon_geometry_type():
                self.layer_combo.addItem(layer.name(), layer)
        if current:
            i = self.layer_combo.findData(current)
            if i >= 0: self.layer_combo.setCurrentIndex(i)
        self.layer_combo.blockSignals(False)
        self.refresh_fields()

    def layer(self):
        return self.layer_combo.currentData()

    def numeric_fields(self):
        layer = self.layer()
        if not layer: return []
        result = []
        for field in layer.fields():
            is_numeric = field.isNumeric
            if bool(is_numeric() if callable(is_numeric) else is_numeric):
                result.append(field.name())
        return result

    def refresh_fields(self):
        fields = self.numeric_fields()
        for row in range(self.svi_table.rowCount()):
            for col in (2, 3, 4):
                combo = self.svi_table.cellWidget(row, col)
                if combo:
                    old = combo.currentText(); combo.blockSignals(True); combo.clear(); combo.addItem("(Do not select)")
                    combo.addItems(fields); idx = combo.findText(old)
                    if idx >= 0: combo.setCurrentIndex(idx)
                    combo.blockSignals(False)
        combo = getattr(self, "lisa_field", None)
        if combo is not None:
            old = combo.currentText()
            combo.clear()
            combo.addItem("(Select a numeric field to analyze)", None)
            for field_name in fields:
                combo.addItem(field_name, field_name)
            idx = combo.findText(old)
            if idx > 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentIndex(0)

    def _field_combo(self):
        c = QComboBox(); c.addItem("(Do not select)"); c.addItems(self.numeric_fields()); return c

    def add_indicator(self):
        r = self.svi_table.rowCount(); self.svi_table.insertRow(r)
        self.svi_table.setItem(r, 0, QTableWidgetItem("Indicator %02d" % (r + 1)))
        mode = QComboBox(); mode.addItems(["Raw value", "Ratio"])
        mode.currentIndexChanged.connect(lambda _value, row=r: self._update_row_mode(row))
        self.svi_table.setCellWidget(r, 1, mode)
        for c in (2, 3, 4): self.svi_table.setCellWidget(r, c, self._field_combo())
        rev = QCheckBox(); rev.setToolTip("Check if higher values indicate lower vulnerability")
        self.svi_table.setCellWidget(r, 5, rev)
        self._update_row_mode(r)

    def _update_row_mode(self, row):
        mode = self.svi_table.cellWidget(row, 1)
        if mode is None:
            return
        use_ratio = mode.currentText() == "Ratio"
        self.svi_table.cellWidget(row, 2).setEnabled(not use_ratio)
        self.svi_table.cellWidget(row, 3).setEnabled(use_ratio)
        self.svi_table.cellWidget(row, 4).setEnabled(use_ratio)

    def remove_indicator(self):
        rows = sorted({i.row() for i in self.svi_table.selectedIndexes()}, reverse=True)
        for r in rows: self.svi_table.removeRow(r)

    def choose_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "GeoPackage output path", "svi_lisa_output.gpkg", "GeoPackage (*.gpkg)")
        if path:
            self._set_output_path(path)

    def _set_output_path(self, path):
        if path and not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        self.output_path = path
        self.svi_path.setText("Output: " + path)
        self.lisa_path.setText("Output: " + path)

    def _ensure_output_path(self, title):
        if self.output_path:
            return True
        path, _ = QFileDialog.getSaveFileName(self, title, "svi_lisa_output.gpkg", "GeoPackage (*.gpkg)")
        if not path:
            return False
        self._set_output_path(path)
        return True

    def _select_fresh_output_path(self):
        """Avoid update-mode failures and preserve previous GeoPackage results."""
        if not self.output_path or not os.path.exists(self.output_path):
            return
        base, ext = os.path.splitext(self.output_path)
        ext = ext or ".gpkg"
        index = 1
        candidate = f"{base}_{index}{ext}"
        while os.path.exists(candidate):
            index += 1
            candidate = f"{base}_{index}{ext}"
        self._set_output_path(candidate)

    def _indicators(self):
        specs = []
        for r in range(self.svi_table.rowCount()):
            name_item = self.svi_table.item(r, 0)
            name = name_item.text().strip() if name_item else "Indicator %02d" % (r + 1)
            mode = self.svi_table.cellWidget(r, 1).currentText()
            raw = self.svi_table.cellWidget(r, 2).currentText()
            num = self.svi_table.cellWidget(r, 3).currentText()
            den = self.svi_table.cellWidget(r, 4).currentText()
            reverse = self.svi_table.cellWidget(r, 5).isChecked()
            if mode == "Raw value" and raw != "(Do not select)":
                specs.append((name, raw, None, None, reverse))
            elif mode == "Ratio" and num != "(Do not select)" and den != "(Do not select)":
                specs.append((name, None, num, den, reverse))
        return specs

    def _values_for(self, layer, specs):
        total = layer.featureCount()
        feats = []
        vals = []
        self._append_log(f"Reading raw values (total {total:,} features)")
        for count, f in enumerate(layer.getFeatures(), 1):
            row = []
            for _, raw, num, den, _ in specs:
                try:
                    a = f[raw] if raw else f[num]
                    b = None if raw else f[den]
                    if a is None or (not raw and (b is None or float(b) == 0)): row.append(None)
                    else:
                        v = float(a) if raw else 100.0 * float(a) / float(b)
                        row.append(v if math.isfinite(v) else None)
                except (TypeError, ValueError, OverflowError): row.append(None)
            feats.append(f)
            vals.append(row)
            if count % 1000 == 0:
                self._append_log(f"Reading raw values: {count:,}/{total:,} features")
        self._append_log(f"Raw value reading complete: {len(feats):,} features")
        return feats, vals

    @staticmethod
    def _percentiles(values, reverse=False):
        valid = [(i, float(v)) for i, v in enumerate(values) if v is not None and math.isfinite(float(v))]
        n = len(valid); out = [None] * len(values)
        if n == 0: return out
        ordered = sorted(valid, key=lambda x: x[1])
        j = 0
        while j < n:
            k = j + 1
            while k < n and ordered[k][1] == ordered[j][1]: k += 1
            # Average rank, scaled to [0, 1]; all equal values receive 0.5.
            p = 0.5 if n == 1 else (((j + 1 + k) / 2.0) - 1.0) / (n - 1.0)
            if reverse: p = 1.0 - p
            for t in range(j, k): out[ordered[t][0]] = p
            j = k
        return out

    def _output_layer(self, source, fields, features, extra_values, name):
        existing_names = {field.name().casefold() for field in source.fields()}
        duplicate_names = sorted({field.name() for field in fields if field.name().casefold() in existing_names})
        if duplicate_names:
            raise ValueError("The input layer already contains output field(s): " + ", ".join(duplicate_names) + ". Select the original input layer or remove existing result fields.")
        mem = QgsVectorLayer(QgsWkbTypes.displayString(source.wkbType()) + "?crs=" + source.crs().authid(), name, "memory")
        dp = mem.dataProvider(); dp.addAttributes(list(source.fields()) + fields); mem.updateFields()
        new_feats = []
        for i, f in enumerate(features):
            nf = QgsFeature(mem.fields()); nf.setGeometry(f.geometry())
            nf.setAttributes(f.attributes() + extra_values[i]); new_feats.append(nf)
        dp.addFeatures(new_feats); mem.updateExtents()
        path = self.output_path
        if path:
            opts = QgsVectorFileWriter.SaveVectorOptions(); opts.driverName = "GPKG"; opts.fileEncoding = "UTF-8"
            opts.layerName = name
            # Create a new GeoPackage for the first result, then add/replace
            # result layers without replacing the file.
            action_enum = getattr(QgsVectorFileWriter, "ActionOnExistingFile", None)
            if os.path.exists(path):
                opts.actionOnExistingFile = getattr(
                    QgsVectorFileWriter, "CreateOrOverwriteLayer",
                    action_enum.CreateOrOverwriteLayer if action_enum else None)
            else:
                opts.actionOnExistingFile = getattr(
                    QgsVectorFileWriter, "CreateOrOverwriteFile",
                    action_enum.CreateOrOverwriteFile if action_enum else None)
            if opts.actionOnExistingFile is None:
                raise RuntimeError("This QGIS API does not support saving individual layers to a GeoPackage.")
            result = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsProject.instance().transformContext(), opts)
            if result[0] != 0: raise RuntimeError(str(result))
            out = QgsVectorLayer(path + "|layername=" + name, name, "ogr")
            if not out.isValid(): raise RuntimeError("The output GeoPackage could not be opened.")
        else: out = mem
        QgsProject.instance().addMapLayer(out)
        return out

    def _style(self, layer, field, class_count=5):
        """Style SVI with Jenks natural breaks and numeric legend ranges."""
        values = []
        for feature in layer.getFeatures():
            value = feature[field]
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                values.append(number)
        if not values:
            return

        jenks = QgsClassificationJenks()
        # This overload is available in QGIS 3.10+ and remains in QGIS 4.
        classes = list(jenks.classes(layer, field, min(class_count, len(set(values)))))
        if not classes:
            return

        colors = ["#ffffcc", "#c2e699", "#78c679", "#31a354", "#006837"]
        descriptors = ["Very low", "Low", "Medium", "High", "Very high"]
        ranges = []
        for index, jenks_range in enumerate(classes):
            lower = float(jenks_range.lowerBound())
            upper = float(jenks_range.upperBound())
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(QColor(colors[min(index, len(colors) - 1)]))
            descriptor = descriptors[min(index, len(descriptors) - 1)]
            label = f"{lower:.3f} – {upper:.3f} ({descriptor})"
            ranges.append(QgsRendererRange(lower, upper, symbol, label))

        renderer = QgsGraduatedSymbolRenderer(field, ranges)
        renderer.setClassificationMethod(QgsClassificationJenks())
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def calculate_svi(self):
        layer = self.layer(); specs = self._indicators()
        if not layer or not specs:
            QMessageBox.warning(self, "Missing input", "Select a polygon layer and at least one indicator (raw value or numerator and denominator)."); return
        if self.auto_lisa.isChecked() and not self._ensure_output_path("SVI and LISA output path"):
            return
        self._select_fresh_output_path()
        self._output_session_active = True
        self._start_log("SVI calculation")
        self._append_log(f"Input layer: {layer.name()} ({layer.featureCount():,} features)")
        self._append_log(f"Number of indicators: {len(specs)}")
        if self.output_path:
            self._append_log(f"Output: {self.output_path}")
        try:
            feats, raw = self._values_for(layer, specs)
            percs = []
            for j, spec in enumerate(specs):
                self._append_log(f"Ranking indicator {j + 1}/{len(specs)} ({spec[0]})")
                percs.append(self._percentiles([r[j] for r in raw], spec[4]))
                self._append_log(f"Indicator ranking complete: {j + 1}/{len(specs)}")
            means = []
            for i in range(len(feats)):
                row = [percs[j][i] for j in range(len(specs))]
                means.append(sum(row) / len(row) if all(v is not None for v in row) else None)
                if (i + 1) % 1000 == 0:
                    self._append_log(f"Calculating SVI sum: {i + 1:,}/{len(feats):,} features")
            if not any(value is not None for value in means):
                raise ValueError("No features have valid values for all selected indicators. Check source values, numerator, denominator, and missing data.")
            self._append_log("Ranking the combined SVI values.")
            svi_rank = self._percentiles(means)
            contrib_names, contrib_scores = [], []
            for i in range(len(feats)):
                if means[i] is None: contrib_names.append(None); contrib_scores.append(None); continue
                best = max(percs[j][i] for j in range(len(specs)))
                names = [specs[j][0] for j in range(len(specs)) if abs(percs[j][i] - best) < 1e-12]
                contrib_names.append(", ".join(names)); contrib_scores.append(best)
            out_fields = []
            data_cols = []
            for j, spec in enumerate(specs):
                safe = "ind%02d" % (j+1)
                out_fields += [QgsField(safe + "_raw", _field_type("Double")), QgsField(safe + "_pct", _field_type("Double"))]
                # Keep one feature-length column per indicator. Each feature
                # contributes its raw value and percentile as two attributes.
                data_cols.append([[raw[i][j], percs[j][i]] for i in range(len(feats))])
            out_fields += [QgsField("svi_sum", _field_type("Double")), QgsField("svi_mean", _field_type("Double")), QgsField("svi_pct", _field_type("Double")), QgsField("max_ind", _field_type("QString"), len=254), QgsField("max_pct", _field_type("Double"))]
            extras=[]
            for i in range(len(feats)):
                extras.append([x for col in data_cols for x in col[i]] + [sum(percs[j][i] for j in range(len(specs))) if means[i] is not None else None, means[i], svi_rank[i], contrib_names[i], contrib_scores[i]])
                if (i + 1) % 1000 == 0:
                    self._append_log(f"Preparing output attributes: {i + 1:,}/{len(feats):,} features")
            self._append_log("Writing the SVI layer to the GeoPackage.")
            out = self._output_layer(layer, out_fields, feats, extras, layer.name() + "_SVI")
            self._append_log("SVI layer write complete.")
            self._style(out, "svi_pct")
            self._append_log("Applied the natural breaks style to the SVI layer.")
            self._append_log("Writing the descriptive statistics CSV.")
            self._write_stats(specs, raw, percs, means, svi_rank)
            self._append_log("Descriptive statistics CSV write complete.")
            if self.auto_lisa.isChecked():
                self.layer_combo.addItem(out.name(), out)
                self.layer_combo.setCurrentIndex(self.layer_combo.count() - 1)
                self.refresh_fields()
                self.lisa_field.setCurrentText("svi_sum")
                self.tabs.setCurrentIndex(1)
                # Save LISA as a second layer in the same GeoPackage as SVI.
                self._append_log("Calculating LISA on the combined SVI.")
                self.calculate_lisa()
            else:
                self._append_log("SVI calculation complete.")
                QMessageBox.information(self, "Complete", "The SVI layer was added. Indicator percentiles and descriptive statistics were exported to CSV. The maximum indicator is the one with the highest indicator percentile; this is not a causal contribution.")
        except Exception as e:
            self._append_log("Error: " + str(e))
            QMessageBox.critical(self, "SVI calculation error", str(e))
        finally:
            self._output_session_active = False
            self._finish_log("Processing finished.")

    def _write_stats(self, specs, raw, percs, means, ranks):
        path = (self.output_path[:-5] if self.output_path and self.output_path.lower().endswith(".gpkg") else self.output_path) if self.output_path else None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save descriptive statistics CSV", "svi_descriptive_statistics.csv", "CSV (*.csv)")
            if not path: return
        if not path.lower().endswith(".csv"): path += "_statistics.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp); w.writerow(["Variable", "Valid count", "Missing count", "Mean", "Standard deviation", "Median", "Minimum", "Maximum"])
            columns = [(specs[j][0] + " (raw value/ratio)", [r[j] for r in raw]) for j in range(len(specs))]
            columns += [(specs[j][0] + " (percentile)", percs[j]) for j in range(len(specs))]
            sums = [value * len(specs) if value is not None else None for value in means]
            max_pct = []
            for i in range(len(means)):
                row = [col[i] for col in percs]
                max_pct.append(max(row) if row and all(value is not None for value in row) else None)
            columns += [("SVI sum", sums), ("SVI mean", means), ("Final SVI percentile", ranks), ("Maximum indicator percentile", max_pct)]
            for name, seq in columns:
                a = sorted(float(x) for x in seq if x is not None and math.isfinite(float(x)))
                if not a: w.writerow([name, 0, len(seq), "", "", "", "", ""]); continue
                n=len(a); med=(a[(n-1)//2]+a[n//2])/2
                sd=math.sqrt(sum((x-sum(a)/n)**2 for x in a)/(n-1)) if n>1 else 0
                w.writerow([name,n,len(seq)-n,sum(a)/n,sd,med,a[0],a[-1]])

    def calculate_lisa(self):
        layer = self.layer()
        field = self.lisa_field.currentData()
        if not layer or not field:
            QMessageBox.warning(self, "Missing input", "Select a polygon layer and a numeric field to analyze."); return
        owns_output_session = not self._output_session_active
        if owns_output_session:
            if not self._ensure_output_path("LISA output path"):
                return
            self._select_fresh_output_path()
            self._output_session_active = True
            self._start_log("LISA calculation")
        self._append_log(f"Analysis layer: {layer.name()} / Field: {field}")
        self._append_log(f"Permutations: {self.perm_spin.value():,} / Neighbors: {self.k_spin.value()} / FDR correction: {'On' if self.fdr_check.isChecked() else 'Off'}")
        if self.output_path:
            self._append_log(f"Output: {self.output_path}")
        try:
            idx=layer.fields().indexFromName(field)
            if idx < 0:
                QMessageBox.warning(self, "Check field", "The selected numeric field is not in the input layer. Select the layer again, then reselect the field."); return
            feats=[]
            valid=[]; ys=[]
            total=layer.featureCount()
            self._append_log(f"Checking input features (total {total:,} features)")
            for i,f in enumerate(layer.getFeatures()):
                feats.append(f)
                try: v=float(f[idx]); ok=math.isfinite(v)
                except (TypeError,ValueError): ok=False; v=0
                if f.geometry().isNull() or f.geometry().isEmpty():
                    ok=False
                if ok: valid.append(i); ys.append(v)
                if (i + 1) % 1000 == 0:
                    self._append_log(f"Input check: {i + 1:,}/{total:,} features")
            n=len(valid)
            if n < 4: raise ValueError("At least four features with valid numeric values are required.")
            if n > 20000: raise ValueError("The analysis is limited to 20,000 features. Reduce the study area.")
            self._append_log(f"Valid features: {n:,} / Missing values or empty geometries: {len(feats) - n:,}")
            if layer.crs().isGeographic():
                raise ValueError("The input layer uses a geographic coordinate system. Reproject it to a suitable projected coordinate system before analysis so neighbor distances can be calculated.")
            mean=sum(ys)/n; z=[v-mean for v in ys]; m2=sum(v*v for v in z)/n
            if m2 == 0: raise ValueError("The analysis field has no variation.")
            subset_feats=[feats[i] for i in valid]
            self._append_log("Creating representative points and a neighbor index.")
            centers=[]
            for position, feature in enumerate(subset_feats, 1):
                centers.append(feature.geometry().pointOnSurface().asPoint())
                if position % 1000 == 0:
                    self._append_log(f"Creating representative points: {position:,}/{n:,} features")
            # Index representative points, not polygon bounding boxes. Otherwise
            # nearest-neighbour queries can rank large/irregular polygons by bbox distance.
            index=QgsSpatialIndex()
            for position, (source_feature, center) in enumerate(zip(subset_feats, centers), 1):
                point_feature=QgsFeature()
                point_feature.setId(source_feature.id())
                point_feature.setGeometry(QgsGeometry.fromPointXY(center))
                index.addFeature(point_feature)
                if position % 1000 == 0:
                    self._append_log(f"Building neighbor index: {position:,}/{n:,} features")
            self._append_log("Neighbor index creation complete.")
            neighbors=[]
            fid_to_pos={f.id():q for q,f in enumerate(subset_feats)}
            for i,f in enumerate(subset_feats):
                nearest=index.nearestNeighbor(centers[i], min(self.k_spin.value()+1, n))
                near=[]
                for fid in nearest:
                    j=fid_to_pos.get(fid)
                    if j is not None and j != i and j not in near: near.append(j)
                if len(near) < min(self.k_spin.value(), n-1):
                    # Rare fallback for tied/degenerate point geometries.
                    ds=sorted((math.hypot(centers[i].x()-centers[j].x(), centers[i].y()-centers[j].y()),j) for j in range(n) if j!=i)
                    near=[j for _,j in ds[:min(self.k_spin.value(),n-1)]]
                neighbors.append(near[:min(self.k_spin.value(), n-1)])
                if (i + 1) % 1000 == 0:
                    self._append_log(f"Finding neighbors: {i + 1:,}/{n:,} features")
            lag=[sum(z[j] for j in js)/len(js) if js else 0 for i,js in enumerate(neighbors)]
            Ii=[z[i]*lag[i]/m2 for i in range(n)]
            observed=[]; pvals=[]; rng=random.Random(20261106); P=self.perm_spin.value()
            self._append_log("Running permutation tests for local Moran’s I.")
            report_every=max(1, min(250, n // 20 or 1))
            for i,js in enumerate(neighbors):
                extreme=0
                for _ in range(P):
                    # Draw from n-1 positions, mapping around the focal observation.
                    # This avoids rebuilding an O(n) candidate list for every feature.
                    picks=rng.sample(range(n-1), min(len(js), n-1))
                    sample=[z[pick if pick < i else pick+1] for pick in picks]
                    perm=z[i]*(sum(sample)/len(sample))/m2
                    if abs(perm) >= abs(Ii[i]): extreme += 1
                observed.append(Ii[i]); pvals.append((extreme+1)/(P+1))
                if (i + 1) % report_every == 0 or i + 1 == n:
                    self._append_log(f"Permutation test: {i + 1:,}/{n:,} features ({(i + 1) * 100.0 / n:.1f}%)")
            qvals=self._bh(pvals)
            alpha=self.alpha_spin.value()
            nominal_count=sum(value <= alpha for value in pvals)
            adjusted_count=sum(value <= alpha for value in qvals)
            self._append_log(f"Permutation tests complete. Features with unadjusted p values at or below alpha: {nominal_count:,}")
            if self.fdr_check.isChecked():
                self._append_log(f"Significant features after FDR correction: {adjusted_count:,} / Minimum adjusted p value: {min(qvals):.6f}")
                if adjusted_count == 0 and nominal_count > 0:
                    self._append_log(f"Some features are significant before correction but not after FDR correction. With {P:,} permutations, the minimum p value increment is {1.0 / (P + 1):.6f}. For many simultaneous tests, consider the effect of the number of permutations.")
            result_by_idx={}
            for i,source_idx in enumerate(valid):
                sig=(qvals[i] <= alpha) if self.fdr_check.isChecked() else (pvals[i] <= alpha)
                cluster="Not significant"
                if sig:
                    if z[i] >= 0 and lag[i] >= 0: cluster="High-High"
                    elif z[i] < 0 and lag[i] < 0: cluster="Low-Low"
                    elif z[i] >= 0 and lag[i] < 0: cluster="High-Low"
                    else: cluster="Low-High"
                result_by_idx[source_idx]=[Ii[i],pvals[i],qvals[i],cluster]
            cluster_counts=Counter(value[3] for value in result_by_idx.values())
            self._append_log("Cluster categories: " + ", ".join(f"{label} {count:,}" for label, count in sorted(cluster_counts.items())))
            extras=[result_by_idx.get(i,[None,None,None,"No data"]) for i in range(len(feats))]
            # Include all six legend categories, including zero-count categories,
            # in the Excel-compatible statistics CSV.  The summary denominator
            # is all input features; the analysis denominator excludes No data.
            lisa_counts=Counter(values[3] for values in extras)
            self._write_lisa_summary(lisa_counts, n, len(feats), append_existing=not owns_output_session)
            outfields=[QgsField("local_I",_field_type("Double")),QgsField("p_perm",_field_type("Double")),QgsField("p_fdr",_field_type("Double")),QgsField("lisa_cluster",_field_type("QString"),len=30)]
            self._append_log("Writing the LISA result layer to the GeoPackage.")
            out=self._output_layer(layer,outfields,feats,extras,layer.name()+"_LISA")
            self._style_clusters(out)
            self._append_log("LISA result layer write complete.")
            QMessageBox.information(self,"Complete","The LISA layer was saved to the GeoPackage.\nPath: %s\nLayer: %s\nPermutations=%d, neighbors=%d."%(self.output_path,out.name(),P,self.k_spin.value()))
        except Exception as e:
            self._append_log("Error: " + str(e))
            QMessageBox.critical(self,"LISA calculation error",str(e))
        finally:
            if owns_output_session:
                self._output_session_active = False
                self._finish_log("LISA calculation finished.")

    def _write_lisa_summary(self, counts, analysis_count, total_count, append_existing=False):
        """Append LISA category counts to the Excel-compatible summary CSV."""
        path = (self.output_path[:-5] if self.output_path and self.output_path.lower().endswith(".gpkg") else self.output_path) if self.output_path else None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save LISA summary", "lisa_category_summary.csv", "CSV (*.csv)")
            if not path:
                self._append_log("The LISA category summary was not saved.")
                return
        if not path.lower().endswith(".csv"):
            path += "_statistics.csv"
        categories = ["High-High", "Low-Low", "High-Low", "Low-High", "Not significant", "No data"]
        file_exists = append_existing and os.path.exists(path)
        encoding = "utf-8" if file_exists else "utf-8-sig"
        with open(path, "a" if file_exists else "w", newline="", encoding=encoding) as fp:
            writer = csv.writer(fp)
            if file_exists:
                writer.writerow([])
            writer.writerow(["LISA category counts"])
            writer.writerow(["Category", "Feature count", "Analysis features", "All features", "Percent of all features (%)"])
            for category in categories:
                count = counts.get(category, 0)
                percent = (100.0 * count / total_count) if total_count else 0.0
                writer.writerow([category, count, analysis_count, total_count, percent])
            writer.writerow(["Total", sum(counts.get(category, 0) for category in categories), analysis_count, total_count, 100.0 if total_count else 0.0])
        self._append_log("Appended LISA category counts to the summary CSV: " + path)

    @staticmethod
    def _bh(p):
        n=len(p); order=sorted(range(n),key=lambda i:p[i]); q=[1.0]*n; running=1.0
        for rank0 in range(n-1,-1,-1):
            i=order[rank0]; rank=rank0+1; running=min(running,p[i]*n/rank); q[i]=min(1.0,running)
        return q

    def _style_clusters(self, layer):
        colors={"High-High":"#b2182b","Low-Low":"#2166ac","High-Low":"#ef8a62","Low-High":"#67a9cf","Not significant":"#d9d9d9","No data":"#ffffff"}
        cats=[]
        for label,color in colors.items():
            sym=QgsSymbol.defaultSymbol(layer.geometryType()); sym.setColor(QColor(color)); cats.append(QgsRendererCategory(label,sym,label))
        layer.setRenderer(QgsCategorizedSymbolRenderer("lisa_cluster",cats)); layer.triggerRepaint()
