# -*- coding: utf-8 -*-
"""SVI and LISA experimental QGIS plugin."""
import csv
from datetime import datetime
import math
import os
from collections import Counter

import numpy as np

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
    raise RuntimeError("QGISのフィールド型を取得できませんでした: " + name)


def _polygon_geometry_type():
    """Return the scoped geometry enum used by supported QGIS releases."""
    return Qgis.GeometryType.Polygon


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
        self.action.setStatusTip("SVIの算出と局所Moran’s I（LISA）を実行します")
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
        self.output_base_path = None
        self.log_dialog = None
        self.log_output = None
        self._log_close_button = None
        self._log_buttons = []
        root = QVBoxLayout(self)
        note = QLabel("ポリゴンレイヤを対象に、SVIの算出またはLISA単独分析を行います。")
        note.setWordWrap(True)
        root.addWidget(note)
        form = QFormLayout()
        self.layer_combo = QComboBox()
        form.addRow("入力ポリゴンレイヤ", self.layer_combo)
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
        layout.addWidget(QLabel("指標ごとに元データまたは分子・分母を指定します。逆転項目はチェックしてください。"))
        self.svi_table = QTableWidget(0, 6)
        self.svi_table.setHorizontalHeaderLabels(["指標名", "計算方法", "元データ", "分子", "分母", "逆転"])
        self.svi_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.svi_table)
        row = QHBoxLayout()
        add = QPushButton("指標を追加"); add.clicked.connect(self.add_indicator)
        remove = QPushButton("選択行を削除"); remove.clicked.connect(self.remove_indicator)
        row.addWidget(add); row.addWidget(remove); row.addStretch(1)
        layout.addLayout(row)
        self.add_indicator()
        self.svi_path = QLabel("出力先: 一時レイヤ")
        path_btn = QPushButton("出力先を選択"); path_btn.clicked.connect(self.choose_output)
        self.output_path = None
        outrow = QHBoxLayout(); outrow.addWidget(self.svi_path); outrow.addWidget(path_btn)
        layout.addLayout(outrow)
        self.auto_lisa = QCheckBox("SVI計算後に、svi_sumのLISAも続けて計算する")
        self.auto_lisa.setChecked(True)
        layout.addWidget(self.auto_lisa)
        calc = QPushButton("SVIを計算してレイヤを追加")
        calc.clicked.connect(self.calculate_svi)
        self._log_buttons.append(calc)
        layout.addWidget(calc)
        self.tabs.addTab(tab, "SVIを計算")

    def _build_lisa_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.lisa_field = QComboBox(); form.addRow("分析する数値フィールド", self.lisa_field)
        self.k_spin = QSpinBox(); self.k_spin.setRange(1, 100); self.k_spin.setValue(8)
        form.addRow("近傍数 k（最近隣）", self.k_spin)
        self.perm_spin = QSpinBox(); self.perm_spin.setRange(99, 9999); self.perm_spin.setSingleStep(100); self.perm_spin.setValue(999)
        form.addRow("置換検定回数", self.perm_spin)
        self.alpha_spin = QDoubleSpinBox(); self.alpha_spin.setRange(0.001, 0.2); self.alpha_spin.setDecimals(3); self.alpha_spin.setSingleStep(0.01); self.alpha_spin.setValue(0.05)
        form.addRow("有意水準 α", self.alpha_spin)
        layout.addLayout(form)
        self.adjustment_combo = QComboBox()
        self.adjustment_combo.addItem("ESS補正BH法（既定）", "ess_bh")
        self.adjustment_combo.addItem("Benjamini–Hochberg法", "bh")
        self.adjustment_combo.addItem("補正なし（未補正p値）", "none")
        self.adjustment_combo.setToolTip(
            "ESS補正BH法では、分析値とk近傍の空間重みからAPLE係数とESSを算出し、\n"
            "論文提案のESS調整済みBH閾値を使用します。"
        )
        form.addRow("多重検定補正", self.adjustment_combo)
        self.lisa_path = QLabel("出力先: 一時レイヤ")
        lisa_path_btn = QPushButton("出力先を選択"); lisa_path_btn.clicked.connect(self.choose_output)
        row = QHBoxLayout(); row.addWidget(self.lisa_path); row.addWidget(lisa_path_btn); layout.addLayout(row)
        explain = QLabel("k近傍法・行標準化を使用します。初期設定はESS補正BH法です。結果には通常BH法とESS補正BH法の補正p値、APLE係数、ESS、選択した判定方法、クラスター区分を保存します。ESS補正BH法は論文提案法で、設定した有意水準でのFDR制御を保証するものではありません。")
        explain.setWordWrap(True); layout.addWidget(explain)
        calc = QPushButton("LISAを計算してレイヤを追加")
        calc.clicked.connect(self.calculate_lisa)
        self._log_buttons.append(calc)
        layout.addWidget(calc); layout.addStretch(1)
        self.tabs.addTab(tab, "LISAを単独実行")

    def _start_log(self, title):
        if self.log_dialog is None:
            self.log_dialog = QDialog(self)
            self.log_dialog.setWindowTitle("G-CHAM SVI and LISA — 計算ログ")
            self.log_dialog.resize(600, 340)
            layout = QVBoxLayout(self.log_dialog)
            self.log_output = QPlainTextEdit(self.log_dialog)
            self.log_output.setReadOnly(True)
            self.log_output.document().setMaximumBlockCount(500)
            layout.addWidget(self.log_output)
            self._log_close_button = QPushButton("閉じる", self.log_dialog)
            self._log_close_button.clicked.connect(self.log_dialog.hide)
            layout.addWidget(self._log_close_button)
        self.log_output.clear()
        self.log_dialog.setWindowTitle("G-CHAM SVI and LISA — " + title + "ログ")
        self.log_dialog.show()
        self.log_dialog.raise_()
        self.log_dialog.activateWindow()
        self._log_close_button.setEnabled(False)
        for button in self._log_buttons:
            button.setEnabled(False)
        self._append_log(title + "を開始しました。")

    def _append_log(self, message):
        if self.log_output is None:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.appendPlainText(f"[{timestamp}] {message}")
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        )

    def _finish_log(self, message):
        self._append_log(message)
        self._log_close_button.setEnabled(True)
        for button in self._log_buttons:
            button.setEnabled(True)

    def refresh_layers(self):
        # Keep stable layer IDs in itemData instead of QgsVectorLayer wrappers.
        # A wrapper can become stale when a layer is removed or replaced while
        # this dialog remains open between repeated runs.
        current = self.layer_combo.currentData()
        self.layer_combo.blockSignals(True); self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.isValid() and QgsWkbTypes.geometryType(layer.wkbType()) == _polygon_geometry_type():
                self.layer_combo.addItem(layer.name(), layer.id())
        if current:
            i = self.layer_combo.findData(current)
            if i >= 0: self.layer_combo.setCurrentIndex(i)
        self.layer_combo.blockSignals(False)
        self.refresh_fields()

    def layer(self):
        layer_id = self.layer_combo.currentData()
        if not layer_id:
            return None
        layer = QgsProject.instance().mapLayer(str(layer_id))
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            return layer
        return None

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
                    old = combo.currentText(); combo.blockSignals(True); combo.clear(); combo.addItem("（選択しない）")
                    combo.addItems(fields); idx = combo.findText(old)
                    if idx >= 0: combo.setCurrentIndex(idx)
                    combo.blockSignals(False)
        combo = getattr(self, "lisa_field", None)
        if combo is not None:
            old = combo.currentText()
            combo.clear()
            combo.addItem("（分析する数値フィールドを選択）", None)
            for field_name in fields:
                combo.addItem(field_name, field_name)
            idx = combo.findText(old)
            if idx > 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentIndex(0)

    def _field_combo(self):
        c = QComboBox(); c.addItem("（選択しない）"); c.addItems(self.numeric_fields()); return c

    def add_indicator(self):
        r = self.svi_table.rowCount(); self.svi_table.insertRow(r)
        self.svi_table.setItem(r, 0, QTableWidgetItem("指標%02d" % (r + 1)))
        mode = QComboBox(); mode.addItems(["元データ", "割合"])
        mode.currentIndexChanged.connect(lambda _value, row=r: self._update_row_mode(row))
        self.svi_table.setCellWidget(r, 1, mode)
        for c in (2, 3, 4): self.svi_table.setCellWidget(r, c, self._field_combo())
        rev = QCheckBox(); rev.setToolTip("数値が高いほど脆弱性が低い場合にチェック")
        self.svi_table.setCellWidget(r, 5, rev)
        self._update_row_mode(r)

    def _update_row_mode(self, row):
        mode = self.svi_table.cellWidget(row, 1)
        if mode is None:
            return
        use_ratio = mode.currentText() == "割合"
        self.svi_table.cellWidget(row, 2).setEnabled(not use_ratio)
        self.svi_table.cellWidget(row, 3).setEnabled(use_ratio)
        self.svi_table.cellWidget(row, 4).setEnabled(use_ratio)

    def remove_indicator(self):
        rows = sorted({i.row() for i in self.svi_table.selectedIndexes()}, reverse=True)
        for r in rows: self.svi_table.removeRow(r)

    def choose_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "GeoPackage出力先", "svi_lisa_output.gpkg", "GeoPackage (*.gpkg)")
        if path:
            self._set_output_path(path, update_base=True)

    def _set_output_path(self, path, update_base=False):
        if path and not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        self.output_path = path
        if update_base or self.output_base_path is None:
            self.output_base_path = path
        self.svi_path.setText("出力先: " + path)
        self.lisa_path.setText("出力先: " + path)

    def _ensure_output_path(self, title):
        if self.output_path:
            return True
        path, _ = QFileDialog.getSaveFileName(self, title, "svi_lisa_output.gpkg", "GeoPackage (*.gpkg)")
        if not path:
            return False
        self._set_output_path(path, update_base=True)
        return True

    def _select_fresh_output_path(self):
        """Use a unique timestamped GeoPackage path for every run."""
        base_path = self.output_base_path or self.output_path
        if not base_path:
            return
        base, ext = os.path.splitext(base_path)
        ext = ext or ".gpkg"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        candidate = f"{base}_{timestamp}{ext}"
        index = 1
        while os.path.exists(candidate):
            candidate = f"{base}_{timestamp}_{index}{ext}"
            index += 1
        self._set_output_path(candidate, update_base=False)

    def _indicators(self):
        specs = []
        for r in range(self.svi_table.rowCount()):
            name_item = self.svi_table.item(r, 0)
            name = name_item.text().strip() if name_item else "指標%02d" % (r + 1)
            mode = self.svi_table.cellWidget(r, 1).currentText()
            raw = self.svi_table.cellWidget(r, 2).currentText()
            num = self.svi_table.cellWidget(r, 3).currentText()
            den = self.svi_table.cellWidget(r, 4).currentText()
            reverse = self.svi_table.cellWidget(r, 5).isChecked()
            if mode == "元データ" and raw != "（選択しない）":
                specs.append((name, raw, None, None, reverse))
            elif mode == "割合" and num != "（選択しない）" and den != "（選択しない）":
                specs.append((name, None, num, den, reverse))
        return specs

    def _values_for(self, layer, specs):
        total = layer.featureCount()
        feats = []
        vals = []
        self._append_log(f"元データを読み込み中（全{total:,}地物）")
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
                self._append_log(f"元データ読み込み: {count:,}/{total:,}地物")
        self._append_log(f"元データ読み込み完了: {len(feats):,}地物")
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
            raise ValueError("入力レイヤに出力予定のフィールドが既にあります: " + ", ".join(duplicate_names) + "。元の入力レイヤを選ぶか、既存の計算結果フィールドを削除してください。")
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
                raise RuntimeError("GeoPackageのレイヤ単位保存に対応するQGIS APIが見つかりません。")
            result = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsProject.instance().transformContext(), opts)
            if result[0] != 0: raise RuntimeError(str(result))
            out = QgsVectorLayer(path + "|layername=" + name, name, "ogr")
            if not out.isValid(): raise RuntimeError("出力したGeoPackageを開けませんでした。")
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
        descriptors = ["低", "やや低", "中", "やや高", "高"]
        ranges = []
        for index, jenks_range in enumerate(classes):
            lower = float(jenks_range.lowerBound())
            upper = float(jenks_range.upperBound())
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(QColor(colors[min(index, len(colors) - 1)]))
            descriptor = descriptors[min(index, len(descriptors) - 1)]
            label = f"{lower:.3f} – {upper:.3f}（{descriptor}）"
            ranges.append(QgsRendererRange(lower, upper, symbol, label))

        renderer = QgsGraduatedSymbolRenderer(field, ranges)
        renderer.setClassificationMethod(QgsClassificationJenks())
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def calculate_svi(self):
        layer = self.layer(); specs = self._indicators()
        if not layer or not specs:
            QMessageBox.warning(self, "入力不足", "ポリゴンレイヤと、少なくとも1つの指標（元データまたは分子・分母）を指定してください。"); return
        if self.auto_lisa.isChecked() and not self._ensure_output_path("SVI・LISA結果の保存先"):
            return
        self._select_fresh_output_path()
        self._output_session_active = True
        self._start_log("SVI計算")
        self._append_log(f"入力レイヤ: {layer.name()}（{layer.featureCount():,}地物）")
        self._append_log(f"使用指標数: {len(specs)}")
        if self.output_path:
            self._append_log(f"出力先: {self.output_path}")
        try:
            feats, raw = self._values_for(layer, specs)
            percs = []
            for j, spec in enumerate(specs):
                self._append_log(f"指標の順位計算中: {j + 1}/{len(specs)}（{spec[0]}）")
                percs.append(self._percentiles([r[j] for r in raw], spec[4]))
                self._append_log(f"指標の順位計算完了: {j + 1}/{len(specs)}")
            means = []
            for i in range(len(feats)):
                row = [percs[j][i] for j in range(len(specs))]
                means.append(sum(row) / len(row) if all(v is not None for v in row) else None)
                if (i + 1) % 1000 == 0:
                    self._append_log(f"SVI合算: {i + 1:,}/{len(feats):,}地物")
            if not any(value is not None for value in means):
                raise ValueError("選択した指標をすべて計算できる地物がありません。元データ、分子・分母、欠損値を確認してください。")
            self._append_log("SVI合算値を順位化しています。")
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
                    self._append_log(f"出力属性を準備中: {i + 1:,}/{len(feats):,}地物")
            self._append_log("SVIレイヤをGeoPackageに書き込んでいます。")
            out = self._output_layer(layer, out_fields, feats, extras, layer.name() + "_SVI")
            self._append_log("SVIレイヤの書き込みが完了しました。")
            self._style(out, "svi_pct")
            self._append_log("SVIレイヤの自然分類スタイルを設定しました。")
            self._append_log("記述統計CSVを書き込んでいます。")
            self._write_stats(specs, raw, percs, means, svi_rank)
            self._append_log("記述統計CSVの書き込みが完了しました。")
            if self.auto_lisa.isChecked():
                self.layer_combo.addItem(out.name(), out.id())
                self.layer_combo.setCurrentIndex(self.layer_combo.count() - 1)
                self.refresh_fields()
                self.lisa_field.setCurrentText("svi_sum")
                self.tabs.setCurrentIndex(1)
                # Save LISA as a second layer in the same GeoPackage as SVI.
                self._append_log("続けてSVI合算値のLISAを計算します。")
                self.calculate_lisa()
            else:
                self._append_log("SVI計算が完了しました。")
                QMessageBox.information(self, "完了", "SVIレイヤを追加しました。指標別パーセンタイルとSVI記述統計CSVを出力しました。最大寄与指標は最大の指標パーセンタイルを示します。")
        except Exception as e:
            self._append_log("エラー: " + str(e))
            QMessageBox.critical(self, "SVI計算エラー", str(e))
        finally:
            self._output_session_active = False
            self._finish_log("処理を終了しました。")

    def _write_stats(self, specs, raw, percs, means, ranks):
        path = (self.output_path[:-5] if self.output_path and self.output_path.lower().endswith(".gpkg") else self.output_path) if self.output_path else None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "記述統計CSVの保存", "svi_descriptive_statistics.csv", "CSV (*.csv)")
            if not path: return
        if not path.lower().endswith(".csv"): path += "_statistics.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp); w.writerow(["項目", "有効数", "欠損数", "平均", "標準偏差", "中央値", "最小", "最大"])
            columns = [(specs[j][0] + "（元値/割合）", [r[j] for r in raw]) for j in range(len(specs))]
            columns += [(specs[j][0] + "（パーセンタイル）", percs[j]) for j in range(len(specs))]
            sums = [value * len(specs) if value is not None else None for value in means]
            max_pct = []
            for i in range(len(means)):
                row = [col[i] for col in percs]
                max_pct.append(max(row) if row and all(value is not None for value in row) else None)
            columns += [("SVI合計", sums), ("SVI平均", means), ("SVI最終パーセンタイル", ranks), ("最大指標パーセンタイル", max_pct)]
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
            QMessageBox.warning(self, "入力不足", "ポリゴンレイヤと分析する数値フィールドを選択してください。"); return
        owns_output_session = not self._output_session_active
        if owns_output_session:
            if not self._ensure_output_path("LISA結果の保存先"):
                return
            self._select_fresh_output_path()
            self._output_session_active = True
            self._start_log("LISA計算")
        self._append_log(f"分析レイヤ: {layer.name()} / フィールド: {field}")
        method = self.adjustment_combo.currentData()
        method_labels = {"ess_bh": "ESS補正BH法", "bh": "Benjamini–Hochberg法", "none": "補正なし"}
        self._append_log(f"置換回数: {self.perm_spin.value():,} / 近傍数: {self.k_spin.value()} / 判定方法: {method_labels.get(method, method)}")
        if self.output_path:
            self._append_log(f"出力先: {self.output_path}")
        try:
            idx=layer.fields().indexFromName(field)
            if idx < 0:
                QMessageBox.warning(self, "フィールドを確認", "選択した数値フィールドが入力レイヤにありません。レイヤを選び直してから、フィールドも選択し直してください。"); return
            feats=[]
            valid=[]; ys=[]
            total=layer.featureCount()
            self._append_log(f"入力地物を確認中（全{total:,}地物）")
            for i,f in enumerate(layer.getFeatures()):
                feats.append(f)
                try: v=float(f[idx]); ok=math.isfinite(v)
                except (TypeError,ValueError): ok=False; v=0
                if f.geometry().isNull() or f.geometry().isEmpty():
                    ok=False
                if ok: valid.append(i); ys.append(v)
                if (i + 1) % 1000 == 0:
                    self._append_log(f"入力確認: {i + 1:,}/{total:,}地物")
            n=len(valid)
            if n < 4: raise ValueError("有効な数値が4地物未満のため計算できません。")
            if n > 20000: raise ValueError("計算対象は20,000地物までです。対象範囲を絞ってください。")
            self._append_log(f"有効地物: {n:,} / 欠損または空ジオメトリ: {len(feats) - n:,}")
            if layer.crs().isGeographic():
                raise ValueError("入力レイヤが緯度・経度座標系です。距離による近傍判定のため、適切な平面直角座標系などに再投影してから実行してください。")
            mean=sum(ys)/n; z=[v-mean for v in ys]; m2=sum(v*v for v in z)/n
            if m2 == 0: raise ValueError("分析フィールドに変動がありません。")
            subset_feats=[feats[i] for i in valid]
            self._append_log("代表点と近傍インデックスを作成中です。")
            centers=[]
            for position, feature in enumerate(subset_feats, 1):
                centers.append(feature.geometry().pointOnSurface().asPoint())
                if position % 1000 == 0:
                    self._append_log(f"代表点を作成中: {position:,}/{n:,}地物")
            # Index representative points, not polygon bounding boxes. Otherwise
            # nearest-neighbour queries can rank large/irregular polygons by bbox distance.
            index=QgsSpatialIndex()
            for position, (source_feature, center) in enumerate(zip(subset_feats, centers), 1):
                point_feature=QgsFeature()
                point_feature.setId(source_feature.id())
                point_feature.setGeometry(QgsGeometry.fromPointXY(center))
                index.addFeature(point_feature)
                if position % 1000 == 0:
                    self._append_log(f"近傍インデックス作成: {position:,}/{n:,}地物")
            self._append_log("近傍インデックスの作成が完了しました。")
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
                    self._append_log(f"近傍を検索中: {i + 1:,}/{n:,}地物")
            lag=[sum(z[j] for j in js)/len(js) if js else 0 for i,js in enumerate(neighbors)]
            Ii=[z[i]*lag[i]/m2 for i in range(n)]
            observed=[]; pvals=[]; rng=np.random.default_rng(20261106)
            P=self.perm_spin.value()
            self._append_log("局所 Moran’s I の置換検定を実行中です。")
            report_every=max(1, min(250, n // 20 or 1))
            for i,js in enumerate(neighbors):
                extreme=0
                for _ in range(P):
                    # Draw from n-1 positions, mapping around the focal observation.
                    # This avoids rebuilding an O(n) candidate list for every feature.
                    picks = rng.choice(
                        n - 1, size=min(len(js), n - 1), replace=False
                    )
                    sample=[z[pick if pick < i else pick+1] for pick in picks]
                    perm=z[i]*(sum(sample)/len(sample))/m2
                    if abs(perm) >= abs(Ii[i]): extreme += 1
                observed.append(Ii[i]); pvals.append((extreme+1)/(P+1))
                if (i + 1) % report_every == 0 or i + 1 == n:
                    self._append_log(f"置換検定: {i + 1:,}/{n:,}地物（{(i + 1) * 100.0 / n:.1f}%）")
            qvals=self._bh(pvals)
            ap_le = self._aple(z, neighbors)
            ess = self._effective_sample_size(n, ap_le)
            ess_qvals = self._ess_bh(pvals, ess) if ess is not None else None
            applied_method = method
            if method == "ess_bh" and ess_qvals is None:
                applied_method = "bh_fallback"
            applied_label = {
                "ess_bh": "ESS補正BH法",
                "bh": "Benjamini–Hochberg法",
                "bh_fallback": "BH法（ESS適用外）",
                "none": "補正なし",
            }.get(applied_method, applied_method)
            alpha=self.alpha_spin.value()
            nominal_count=sum(value <= alpha for value in pvals)
            selected_qvals = ess_qvals if applied_method == "ess_bh" else qvals
            selected_count = sum(value <= alpha for value in selected_qvals) if applied_method != "none" else nominal_count
            self._append_log(f"置換検定完了。未補正p値が有意水準以下: {nominal_count:,}地物")
            self._append_log(f"APLE係数: {ap_le:.6f} / ESS: {ess:.2f} / 有意水準 α: {alpha:.3f}")
            if applied_method == "bh_fallback":
                self._append_log("APLE係数が正で有効なESSを算出できなかったため、今回の有意判定は通常BH法に切り替えました。負の空間依存にESS補正BH法を適用する妥当性は論文で検討されていません。")
            if applied_method == "none":
                self._append_log(f"補正なしの有意地物: {selected_count:,}")
            else:
                label = "ESS補正BH法" if applied_method == "ess_bh" else "Benjamini–Hochberg法"
                self._append_log(f"{label}で有意な地物: {selected_count:,} / 最小調整p値: {min(selected_qvals):.6f}")
                if selected_count == 0 and nominal_count > 0:
                    self._append_log(f"未補正では有意な地物がありますが、選択した補正後は有意ではありません。置換{P:,}回ではp値の最小刻みが{1.0 / (P + 1):.6f}のため、多数の地物を同時検定する場合は置換回数の影響を確認してください。")
            result_by_idx={}
            for i,source_idx in enumerate(valid):
                if applied_method == "none":
                    sig = pvals[i] <= alpha
                else:
                    use_ess = applied_method == "ess_bh"
                    sig = (ess_qvals[i] if use_ess else qvals[i]) <= alpha
                cluster="Not significant"
                if sig:
                    if z[i] >= 0 and lag[i] >= 0: cluster="High-High"
                    elif z[i] < 0 and lag[i] < 0: cluster="Low-Low"
                    elif z[i] >= 0 and lag[i] < 0: cluster="High-Low"
                    else: cluster="Low-High"
                result_by_idx[source_idx]=[Ii[i],pvals[i],qvals[i],ess_qvals[i] if ess_qvals is not None else None,ap_le,ess,applied_label,cluster]
            # LISA result arrays store the category in position 7; positions
            # 2 and 3 contain the ordinary and ESS adjusted p-values.
            cluster_counts=Counter(value[7] for value in result_by_idx.values())
            self._append_log("クラスター区分: " + ", ".join(f"{label} {count:,}" for label, count in sorted(cluster_counts.items())))
            extras=[result_by_idx.get(i,[None,None,None,None,None,None,applied_label,"No data"]) for i in range(len(feats))]
            # Include all six legend categories, including zero-count categories,
            # in the Excel-compatible statistics CSV.  The summary denominator
            # is all input features; the analysis denominator excludes No data.
            lisa_counts=Counter(values[7] for values in extras)
            self._write_lisa_summary(lisa_counts, n, len(feats), method_labels.get(method, method), applied_label, alpha, ap_le, ess, append_existing=not owns_output_session)
            outfields=[QgsField("local_I",_field_type("Double")),QgsField("p_perm",_field_type("Double")),QgsField("p_fdr",_field_type("Double")),QgsField("p_ess_bh",_field_type("Double")),QgsField("aple_rho",_field_type("Double")),QgsField("ess_n",_field_type("Double")),QgsField("lisa_method",_field_type("QString"),len=30),QgsField("lisa_cluster",_field_type("QString"),len=30)]
            self._append_log("LISA結果レイヤをGeoPackageに書き込んでいます。")
            out=self._output_layer(layer,outfields,feats,extras,layer.name()+"_LISA")
            self._style_clusters(out)
            self._append_log("LISA結果レイヤの書き込みが完了しました。")
            QMessageBox.information(self,"完了","LISAレイヤをGeoPackageに保存しました。\n保存先: %s\nレイヤ: %s\n検定回数=%d、近傍数=%d。"%(self.output_path,out.name(),P,self.k_spin.value()))
        except Exception as e:
            self._append_log("エラー: " + str(e))
            QMessageBox.critical(self,"LISA計算エラー",str(e))
        finally:
            if owns_output_session:
                self._output_session_active = False
                self._finish_log("LISA計算を終了しました。")

    def _write_lisa_summary(self, counts, analysis_count, total_count, selected_method, applied_method, alpha, ap_le, ess, append_existing=False):
        """Append LISA category counts to the Excel-compatible summary CSV."""
        path = (self.output_path[:-5] if self.output_path and self.output_path.lower().endswith(".gpkg") else self.output_path) if self.output_path else None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "LISAサマリーの保存", "lisa_category_summary.csv", "CSV (*.csv)")
            if not path:
                self._append_log("LISAカテゴリ別サマリーは保存されませんでした。")
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
            writer.writerow(["LISA解析設定"])
            writer.writerow(["選択方法", selected_method])
            writer.writerow(["実適用方法", applied_method])
            writer.writerow(["有意水準 α", alpha])
            writer.writerow(["APLE係数", ap_le])
            writer.writerow(["有効サンプルサイズ（ESS）", ess])
            writer.writerow(["分析対象数", analysis_count])
            writer.writerow(["LISAカテゴリ別該当数"])
            writer.writerow(["カテゴリ", "該当数", "分析対象数", "全地物数", "全地物に占める割合（%）"])
            for category in categories:
                count = counts.get(category, 0)
                percent = (100.0 * count / total_count) if total_count else 0.0
                writer.writerow([category, count, analysis_count, total_count, percent])
            writer.writerow(["合計", sum(counts.get(category, 0) for category in categories), analysis_count, total_count, 100.0 if total_count else 0.0])
        self._append_log("LISA解析設定とカテゴリ別該当数をサマリーCSVに追記しました: " + path)

    @staticmethod
    def _bh(p):
        n=len(p); order=sorted(range(n),key=lambda i:p[i]); q=[1.0]*n; running=1.0
        for rank0 in range(n-1,-1,-1):
            i=order[rank0]; rank=rank0+1; running=min(running,p[i]*n/rank); q[i]=min(1.0,running)
        return q

    @staticmethod
    def _aple(z, neighbors):
        """Estimate the global spatial dependence coefficient with APLE."""
        n = len(z)
        if n < 2:
            raise ValueError("APLEの推定には2地物以上が必要です。")
        lag = [sum(z[j] for j in js) / len(js) if js else 0.0 for js in neighbors]
        numerator = sum(z[i] * lag[i] for i in range(n))
        z_norm = sum(value * value for value in z)
        denominator = sum(value * value for value in lag)
        neighbor_sets = [set(js) for js in neighbors]
        trace_w2 = 0.0
        for i, js in enumerate(neighbors):
            ki = len(js)
            if not ki:
                continue
            trace_w2 += sum(1.0 / (ki * len(neighbors[j])) for j in js
                            if i in neighbor_sets[j] and neighbors[j])
        denominator += (trace_w2 / n) * z_norm
        if not math.isfinite(denominator) or denominator <= 0:
            raise ValueError("APLEの分母が0以下または非有限となりました。")
        rho = numerator / denominator
        if not math.isfinite(rho):
            raise ValueError("APLE係数を有限値として推定できませんでした。")
        return rho

    @staticmethod
    def _effective_sample_size(n, rho):
        """Calculate ESS using the formula in the referenced ESS paper."""
        if n < 2 or not math.isfinite(rho) or rho <= 0 or rho >= 1:
            return None
        one_minus = 1.0 - rho
        power_term = (1.0 - rho ** (n - 1)) / n
        denominator = (1.0 + 2.0 * (rho / one_minus) * (1.0 - 1.0 / n)
                       - 2.0 * (rho / (one_minus * one_minus)) * power_term)
        if not math.isfinite(denominator) or denominator <= 0:
            return None
        ess = n / denominator
        if not math.isfinite(ess) or ess <= 0:
            return None
        return ess

    @staticmethod
    def _ess_bh(p, ess):
        """Return ESS adjusted BH q values for thresholds min(k*alpha/ESS, alpha)."""
        n = len(p)
        order = sorted(range(n), key=lambda i: p[i])
        q = [1.0] * n
        running = 1.0
        for rank0 in range(n - 1, -1, -1):
            i = order[rank0]
            rank = rank0 + 1
            running = min(running, p[i] * max(ess / rank, 1.0))
            q[i] = min(1.0, running)
        return q

    def _style_clusters(self, layer):
        colors={"High-High":"#b2182b","Low-Low":"#2166ac","High-Low":"#ef8a62","Low-High":"#67a9cf","Not significant":"#d9d9d9","No data":"#ffffff"}
        cats=[]
        for label,color in colors.items():
            sym=QgsSymbol.defaultSymbol(layer.geometryType()); sym.setColor(QColor(color)); cats.append(QgsRendererCategory(label,sym,label))
        layer.setRenderer(QgsCategorizedSymbolRenderer("lisa_cluster",cats)); layer.triggerRepaint()
