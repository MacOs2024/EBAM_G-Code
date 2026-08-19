from __future__ import annotations
import tempfile
import io
import zipfile
from pathlib import Path
import math
from dataclasses import replace

import streamlit as st
from ebam_gcode_studio import gcode_tools as _gtools
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

from ebam_gcode_studio.core import (
    APP_VERSION, ProcessSettings, MATERIAL_LIBRARY, FIELD_PROVEN_MAX_LAYER_MM,
    max_wire_feed_for_beam, wire_area_from_diameter,
    load_mesh_any, normalize_mesh, mesh_summary,
    generate_from_mesh, generate_from_polygons_2d,
    settings_to_json, settings_from_dict, recommend_settings_from_summary,
    load_polygons_from_csv, load_polygons_from_dxf, create_standard_shape_polygons,
    _section_polygons_at_z, _hatch_segments_for_polygons, _contour_segments_for_polygons,
    polygon_summary, analyze_gcode_reverse, build_gcode_reverse_report,
    bormash_limits_report, is_bormash_profile,
    rotational_shell_summary, rotational_shell_polygons_at_z, generate_rotational_shell,
    rotational_shell_outer_radius, rotational_ring_segments_at_z, rotational_spiral_segments_at_z,
    rotational_layer_radii_at_z, rotary_c_speed_deg_min,
    build_experience_calibration_profile, experience_profile_to_json, experience_profile_to_csv,
    _section_radii_from_polygons_for_rotary_c
)

APP_FILE_TAG = APP_VERSION.replace(".", "")

st.set_page_config(page_title=f"EBAM G-Code Studio {APP_VERSION}", layout="wide")
st.title(f"EBAM G-Code Studio {APP_VERSION}")
_steps = ["1·STL/режим", "2·параметры", "3·проверка", "4·TEST", "5·файл"]
st.caption("Этапы: " + "  →  ".join(_steps) + "  ·  FOCUS-режим и «Принять как базу» помогают идти по шагам.")
st.caption("Простой и расширенный режимы • расчёт • предпросмотр • G-code • обратный анализ")

st.info("Стартовый расчёт для EBAM. Перед полной деталью: предпросмотр, TEST Z10–15 мм, сухой прогон, проверка W-ретракта.")


# ------------------------- widget help tooltips -------------------------
# Streamlit widgets support the `help=` parameter; it displays the small "?"
# tooltip next to editable fields.  This wrapper adds a meaningful tooltip to
# every editable widget even if an individual call below forgets to pass help.
_WIDGET_HELP_TEXTS = {
    "Что нужно сделать?": "Выбор основной задачи: создать новый G-code или только проанализировать уже готовый файл.",
    "Интерфейс": "Простой режим скрывает тонкие настройки. Расширенный режим открывает все технологические параметры для пусконаладки.",
    "Источник": "Откуда брать геометрию детали: параметрическая чаша/баллон, STL, стандартная фигура, DXF или CSV-контур.",
    "Материал": "Материал влияет на стартовые рекомендации по энергии, скорости, плотности и подаче проволоки.",
    "Цель": "Качество — осторожнее и медленнее; Баланс — стартовый режим; Скорость — быстрее, но с большим риском дефектов.",
    "Загрузить G-code для анализа": "Загрузите .ngc/.txt G-code. Приложение восстановит траектории, активные участки E0/E2/W и опасные места.",
    "Зеркально по X": "Отразить 2D-геометрию относительно оси X перед генерацией траекторий.",
    "Зеркально по Y": "Отразить 2D-геометрию относительно оси Y перед генерацией траекторий.",
    "Поворот фигуры": "Повернуть выбранную стандартную 2D-фигуру на столе перед построением слоёв.",
    "Повернуть всю 2D-фигуру, град": "Ручной поворот импортированного DXF/CSV-контура вокруг центра перед генерацией.",
    "Ускоряющее напряжение U, кВ": "Напряжение электронного луча. Используется в расчёте тока E0 через мощность U×I и энергию Дж/мм.",
    "Ускоряющее напряжение, кВ": "Напряжение электронного луча. Чем выше U, тем меньший ток нужен для той же энергии Дж/мм.",
    "Лимит тока пучка E0, мА": "Верхняя граница расчётного тока пучка E0 в простом режиме. Если расчёту нужно больше, приложение предупредит/ограничит по логике режима.",
    "Верхний лимит тока пучка E0, мА": "Максимально разрешённый расчётный ток пучка E0. Повышать только осознанно и проверять на коротком TEST.",
    "Нижний лимит тока пучка E0, мА": "Минимально разрешённый ток E0. Можно ставить 0 мА для диагностических и очень маломощных режимов.",
    "Предупреждать о малом токе ниже, мА": "Если расчётный E0 ниже этого значения, аудит выдаст предупреждение о возможной нестабильности режима.",
    "Контрольная подача проволоки, мм/с": "Граница предупреждения для E2 в простом режиме. Это не физический зажим, а контроль реалистичности подачи.",
    "Нижняя контрольная подача проволоки, мм/с": "Если расчётная подача ниже этой границы, приложение предупредит о слишком малой подаче проволоки.",
    "Верхняя контрольная подача проволоки, мм/с": "Если расчётная подача выше этой границы, приложение предупредит. Сам G-code не обрезается автоматически.",
    "Подогнать режим под желаемое время": "Позволяет попробовать подобрать параметры под заданное время изготовления и показать риск ухудшения качества.",
    "Часы": "Часовая часть желаемого времени изготовления для простого режима.",
    "Минуты": "Минутная часть желаемого времени изготовления для простого режима.",
    "Центрировать XY вокруг нуля": "Сместить деталь относительно X/Y. Для Бормаш обычно выключать, чтобы не уйти в отрицательные координаты.",
    "Добавить 1 контурный проход для края": "Добавляет проход по наружному контуру слоя для лучшего края и формы стенки.",
    "Комментарии в G-code": "Добавляет поясняющие комментарии в G-code. Для больших STL можно выключить, чтобы файл был меньше.",
    "Шаг слоя Z, мм": "Высота подъёма между слоями. Большой Z-шаг ускоряет процесс, но повышает риск недоплава, грубой поверхности и тыкания проволоки.",
    "Шаг дорожек, мм": "Расстояние между соседними траекториями. Это не физическая ширина валика, а шаг укладки дорожек.",
    "Диаметр проволоки, мм": "Диаметр используемой проволоки. Нужен для расчёта площади сечения и подачи E2 в мм/с.",
    "Коэффициент осаждения η": "Доля объёма поданной проволоки, которая фактически формирует полезный валик. 1.0 — верхняя оценка без потерь; лучше определять по калибровочному образцу.",
    "Режим планировщика траектории": "G64 P/Q разрешает плавное сопряжение. G61/G61.1 могут замедлять или останавливать движение на границах сегментов. Режим станка по умолчанию не добавляет команду.",
    "Допуск G64 P, мм": "Максимально допустимое отклонение траектории при сглаживании G64. Малое значение точнее, но может снижать скорость.",
    "Naive CAM G64 Q, мм": "Q=0 отключает упрощение коротких почти коллинеарных сегментов. Для коротких C+Z переходов это безопаснее.",
    "Команды E0/E2 в непрерывном C-режиме": "M68 совместим с текущей конфигурацией, но действует немедленно. M67 синхронизирован со следующим движением и разрешается только после подтверждения HAL.",
    "Подтверждаю поддержку M67 в HAL": "Включать только после проверки, что motion.analog-out для E0/E2 действительно подключены в HAL вашей установки.",
    "Допуск изменения радиуса в no-pause, мм": "Максимальное изменение радиуса по высоте, при котором фиксированный X ещё считается цилиндрическим. При превышении генерация блокируется, чтобы не получить неверную чашу/баллон.",
    "Плотность, г/см³": "Плотность материала нужна для оценки массы, расхода проволоки и отчёта по детали.",
    "Энергия низ, Дж/мм": "Целевая энергия на 1 мм траектории в нижней части детали. Влияет на расчёт тока E0.",
    "Энергия верх, Дж/мм": "Целевая энергия на 1 мм траектории в верхней части детали. Можно снижать/повышать для компенсации накопления тепла.",
    "Скорость низ F, мм/мин": "Скорость движения в нижней части детали. В G-code записывается как F в мм/мин.",
    "Скорость верх F, мм/мин": "Скорость движения в верхней части детали. Может отличаться от нижней для тепловой компенсации.",
    "Фокус E1, мА": "Ток фокусировки электронного луча. Влияет на пятно, форму ванны и ширину/глубину проплавления.",
    "Тип проходов": "Выбор общей стратегии: непрерывная змейка или отдельные параллельные проходы.",
    "Как вести дорожки": "Выбор схемы проходов в простом режиме. Направление Y-/Y+/X-/X+ задаёт первый или все рабочие проходы.",
    "Ось основных дорожек змейки": "Для змейки выбирает, вдоль какой оси идут основные длинные рабочие дорожки.",
    "Первый рабочий проход": "Направление первой рабочей дорожки. Следующие дорожки в змейке будут чередоваться автоматически.",
    "Направление всех параллельных проходов": "Для посегментного режима задаёт одно направление, в котором идут все рабочие дорожки слоя.",
    "Тепловое чередование через одну дорожку": "Меняет порядок проходов для уменьшения локального перегрева. Если нужен понятный прямой порядок, оставьте выключенным.",
    "Чередовать X/Y по слоям": "Поворачивает направление штриховки между слоями, чтобы уменьшить направленность структуры и деформаций.",
    "Скорость перехода змейки между дорожками (×F)": "Множитель скорости для перемычек между дорожками в непрерывной змейке. На этих переходах E2 выключен.",
    "Ширина валика из TEST, мм (0 = не использовать)": "Измеренная ширина реального одиночного валика. Позволяет рассчитать шаг дорожек от фактической наплавки.",
    "Модель перекрытия валиков": "Выбор эмпирической модели расчёта шага дорожек от ширины реального валика.",
    "Брать шаг дорожек из ширины валика": "Если включено, ручной шаг дорожек заменяется шагом, рассчитанным по измеренной ширине TEST-валика.",
    "Отступ от края контура, мм": "Смещение внутренних дорожек от наружной границы слоя, чтобы не выходить за стенку детали.",
    "Контурных проходов на слой": "Количество проходов по контуру слоя. Улучшает край, но увеличивает время и тепловложение.",
    "Контур каждые N слоёв": "Как часто выполнять контурный проход. 1 — каждый слой, больше — реже.",
    "Сначала контур, потом штриховка": "Меняет порядок внутри слоя: сначала обводка края, затем заполнение.",
    "Адаптивная тонкая стенка STL": "Помогает не терять тонкие участки STL: уменьшает отступы/шаг и добавляет проходы там, где обычная штриховка не помещается.",
    "Устойчивый поиск STL-сечения по Z": "Если точное сечение STL пустое, приложение ищет близкий уровень Z. Полезно для тонких/кривых STL.",
    "Коррекция проволоки для тонких слоёв": "При адаптивном уменьшении шага дорожек корректирует E2, чтобы не переливать металл.",
    "Офлайн-резерв построения STL-сечения": "Резервный способ построить сечение STL, если стандартная нарезка не справилась.",
    "Последний резерв: XY-проекция STL": "Аварийный режим для диагностики. Может искажать полые или наклонные модели.",
    "Z-hop, мм": "Подъём Z при безопасных переходах. Уменьшает риск зацепа, но увеличивает время.",
    "Использовать W-ретракт": "Добавляет физический ретракт оси W. Для Бормаш проверять направление W сухим прогоном.",
    "W-ретракт, мм": "Величина отвода/возврата проволоки по оси W при старте/финише участка.",
    "Lead-in без проволоки, мм": "Начальный участок с лучом без подачи проволоки для прогрева/стабилизации ванны.",
    "Мягкий старт, мм": "Длина плавного нарастания тока/подачи в начале дорожки.",
    "Мягкий финиш, мм": "Длина плавного снижения режима в конце дорожки для уменьшения кратера.",
    "Задать желаемое время изготовления": "Включает режим подгонки параметров под заданное время с оценкой качества и ограничений.",
    "Цель, часы": "Часы в целевом времени изготовления для расширенного режима.",
    "Цель, минуты": "Минуты в целевом времени изготовления для расширенного режима.",
    "Как подстраивать": "Выбирает, какие параметры разрешено менять при попытке попасть в заданное время.",
    "Тип фигуры": "Выбор параметрической осесимметричной формы: шар-баллон, чаша, стакан или конус.",
    "Высота Z, мм": "Полная высота параметрической чаши/баллона по оси Z.",
    "Максимальный диаметр, мм": "Наибольший наружный диаметр чаши/баллона. Проверяется по лимитам Бормаш.",
    "Толщина стенки, мм": "Расчётная толщина стенки. Должна быть сопоставима с реальной шириной валика и шагом дорожек.",
    "Толщина закрытого донца, мм": "Высота сплошной нижней части перед переходом к полой стенке.",
    "Диаметр у донца, мм": "Наружный диаметр в нижней зоне шар-баллона около донца.",
    "Диаметр горловины/отверстия сверху, мм": "Наружный диаметр верхней горловины/отверстия шар-баллона.",
    "Где максимальный диаметр по высоте, доля 0..1": "Положение самого широкого места: 0 — низ, 1 — верх. Обычно для баллона около середины.",
    "Плавность раздува": "Управляет кривизной перехода от донца к максимальному диаметру и горловине.",
    "Диаметр дна, мм": "Наружный диаметр нижней части чаши/горшка.",
    "Диаметр раскрытия сверху, мм": "Наружный диаметр верхнего раскрытия чаши или горшка.",
    "Кривизна стенки": "Определяет выпуклость или плавность стенки чаши по высоте.",
    "Нижний диаметр, мм": "Диаметр нижнего основания конусной/цилиндрической фигуры.",
    "Разрешение окружности": "Количество точек аппроксимации окружности. Больше — гладче форма, но больше G-code.",
    "Фигура": "Выбор стандартной 2D-геометрии, которая будет вытянута по Z слоями.",
    "Высота построения Z, мм": "Высота 3D-построения для стандартной 2D-фигуры, DXF или CSV-контура.",
    "Ширина X, мм": "Размер фигуры по оси X.",
    "Длина Y, мм": "Размер фигуры по оси Y.",
    "Сторона, мм": "Длина стороны квадратной стандартной фигуры.",
    "Радиус, мм": "Радиус круглой стандартной фигуры.",
    "Внешний радиус, мм": "Наружный радиус кольца/звезды или другой радиальной фигуры.",
    "Внутренний радиус, мм": "Внутренний радиус отверстия или внутренней впадины фигуры.",
    "Радиус X, мм": "Полуось эллипса по X.",
    "Радиус Y, мм": "Полуось эллипса по Y.",
    "Основание X, мм": "Длина основания треугольника по оси X.",
    "Высота треугольника Y, мм": "Высота треугольника по оси Y.",
    "Количество сторон": "Число сторон правильного многоугольника.",
    "Радиус описанной окружности, мм": "Радиус окружности, на которой лежат вершины многоугольника.",
    "Поворот, град": "Поворот стандартной фигуры вокруг центра.",
    "Лучей": "Количество лучей звезды.",
    "Какие исходные оси станут X/Y/Z": "Переставляет оси STL, если модель загружена лежащей на боку или в другой ориентации.",
    "Rx": "Поворот STL вокруг оси X перед генерацией.",
    "Ry": "Поворот STL вокруг оси Y перед генерацией.",
    "Rz": "Поворот STL вокруг оси Z перед генерацией.",
    "Зеркально STL по X": "Отразить STL-модель по X перед нормализацией и нарезкой.",
    "Зеркально STL по Y": "Отразить STL-модель по Y перед нормализацией и нарезкой.",
    "Зеркально STL по Z": "Отразить STL-модель по Z перед нормализацией и нарезкой.",
    "Быстрый вариант": "Готовые варианты ориентации STL, чтобы быстро поставить модель нужной плоскостью на стол.",
    "-X": "Зеркальное отражение STL по направлению X.",
    "-Y": "Зеркальное отражение STL по направлению Y.",
    "-Z": "Зеркальное отражение STL по направлению Z.",
    "Поворот на столе вокруг Z": "Поворот детали на рабочем столе вокруг вертикальной оси Z.",
    "Сечение по высоте, доля Z": "Выбор высоты слоя для предварительного просмотра траекторий.",
    "Сечение чаши/баллона по высоте, доля Z": "Выбор относительной высоты, на которой показывается сечение и дорожки чаши/баллона.",
    "Сначала сделать короткий TEST-файл": "Генерирует только нижнюю часть детали для первичной проверки режима, геометрии и безопасности.",
    "Высота TEST, мм": "До какой высоты Z формировать короткий тестовый G-code.",
}

_WIDGET_HELP_FALLBACKS = {
    "number_input": "Числовой параметр расчёта. Наведите сюда: значение влияет на геометрию, режим EBAM или проверку безопасности.",
    "selectbox": "Выбор варианта, который влияет на геометрию, режим генерации или отображение.",
    "radio": "Выбор одного режима работы из нескольких. От него зависит логика расчёта или G-code.",
    "checkbox": "Включает или выключает дополнительную функцию расчёта, генерации или проверки.",
    "file_uploader": "Загрузка файла для генерации, анализа или проверки.",
    "slider": "Регулируемый параметр предпросмотра или расчёта.",
}


def _widget_help(label, widget_name: str) -> str:
    label_text = str(label)
    if label_text in _WIDGET_HELP_TEXTS:
        return _WIDGET_HELP_TEXTS[label_text]
    return _WIDGET_HELP_FALLBACKS.get(widget_name, "Изменяемый параметр приложения.")


def _install_default_widget_help() -> None:
    """Attach default help='...' to all editable Streamlit widgets used below."""
    for widget_name in ("number_input", "selectbox", "radio", "checkbox", "file_uploader", "slider"):
        original = getattr(st, widget_name)

        def _wrapped(label, *args, _orig=original, _name=widget_name, **kwargs):
            if not kwargs.get("help"):
                kwargs["help"] = _widget_help(label, _name)
            return _orig(label, *args, **kwargs)

        setattr(st, widget_name, _wrapped)


_install_default_widget_help()

st.markdown(
    """
    <style>
    .block-container {padding-top: 3.2rem; padding-bottom: 2rem;}
    section[data-testid="stSidebar"] .block-container {padding-top: 1rem;}
    h1 {margin-top: 0.2rem; padding-top: 0.2rem;}
    div[data-testid="stMetric"] {background: rgba(128,128,128,0.08); padding: 0.65rem 0.8rem; border-radius: 0.7rem;}
    .ebam-card {border: 1px solid rgba(128,128,128,0.25); border-radius: 0.8rem; padding: 0.85rem 1rem; margin: 0.35rem 0 0.8rem 0;}
    .ebam-small {font-size: 0.92rem; opacity: 0.86;}
    </style>
    """,
    unsafe_allow_html=True,
)

with st.expander("Кратко: как пользоваться", expanded=False):
    st.markdown(
        """
        **Простой режим** — для быстрого расчёта: выбери геометрию, материал и цель `Качество / Баланс / Скорость`.
        Приложение само подбирает Z-шаг, шаг дорожек, скорость, ток, проволоку и паузы.

        **Расширенный режим** — для пусконаладки и экспериментов: открываются все технологические параметры,
        ориентация STL, резервные настройки тонких сечений, контуры, W-ретракт, лимиты, целевое время и JSON-профиль.

        **Безопасный порядок:** предпросмотр → TEST Z10–15 мм → просмотр G-code/аудита → сухой прогон без луча и проволоки → короткая наплавка → полный файл.
        """
    )

# ------------------------- helpers -------------------------
def _tmp_file(uploaded, suffix):
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded.getbuffer())
        return Path(tmp.name)


@st.cache_data(show_spinner=False)
def _cached_mesh_from_bytes(data: bytes, suffix: str = ".stl"):
    """Load mesh once per uploaded file content.

    Streamlit reruns the script after every widget change; caching avoids re-reading
    the same STL again and again while the operator changes process parameters.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return load_mesh_any(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@st.cache_data(show_spinner=False)
def _cached_dxf_polygons_from_bytes(data: bytes):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return load_polygons_from_dxf(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@st.cache_data(show_spinner=False)
def _cached_csv_polygons_from_text(text: str):
    return load_polygons_from_csv(text)


@st.cache_data(show_spinner=False)
def _cached_standard_shape(shape_name: str, params_items: tuple):
    return create_standard_shape_polygons(shape_name, dict(params_items))


def _feed_mm_min_to_speed_mm_s(feed_mm_min: float) -> float:
    return float(feed_mm_min) / 60.0


def _speed_mm_s_to_feed_mm_min(speed_mm_s: float) -> float:
    return float(speed_mm_s) * 60.0


def _make_result_zip(result, settings_json: str, suffix: str) -> bytes:
    """Pack G-code, layer table, audit and settings into one download."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"ebam_generated_{APP_FILE_TAG}_{suffix}.ngc", result.gcode)
        zf.writestr(f"ebam_layers_{APP_FILE_TAG}_{suffix}.csv", result.layer_csv)
        zf.writestr(f"ebam_audit_{APP_FILE_TAG}_{suffix}.txt", result.audit_text)
        zf.writestr(f"ebam_settings_{APP_FILE_TAG}_{suffix}.json", settings_json)
        zf.writestr(
            "README_RESULT_RU.txt",
            f"EBAM G-Code Studio {APP_VERSION}\n"
            "Комплект результата: G-code, таблица слоёв, аудит и JSON-настройки.\n"
            "Перед реальной наплавкой: viewer/simulation -> сухой прогон без луча и проволоки -> проверка W -> TEST Z10-15 мм.\n"
        )
    return buf.getvalue()


def _safety_gate_status(src_type: str, summary, settings: ProcessSettings, time_plan,
                        n_layers: int, wire_bottom: float, wire_top: float,
                        current_bottom_need: float, current_top_need: float):
    """Small operator-oriented status block: can we proceed to TEST?"""
    critical = []
    warn = []
    info = []

    if n_layers <= 0 or float(summary.get("size_z", 0.0) or 0.0) <= 0:
        critical.append("Геометрия не даёт положительной высоты Z или числа слоёв.")
    if current_bottom_need > settings.current_max_ma * 1.001 or current_top_need > settings.current_max_ma * 1.001:
        critical.append("Расчётный ток выше заданного лимита E0: в G-code будет клиппинг тока и энергия получится ниже расчётной.")
    if settings.current_min_ma > 0 and (current_bottom_need < settings.current_min_ma * 0.999 or current_top_need < settings.current_min_ma * 0.999):
        warn.append("Расчётный ток ниже минимального E0: в G-code ток будет поднят до минимума, фактическая энергия будет выше расчётной.")
    elif settings.current_low_warning_ma > 0 and (current_bottom_need < settings.current_low_warning_ma or current_top_need < settings.current_low_warning_ma):
        warn.append("Расчётный ток ниже порога предупреждения малого E0. G-code не меняется, но нужно проверить устойчивость источника на таком токе.")
    if src_type == "STL 3D" and settings.projection_fallback_if_empty:
        warn.append("Включён последний резервный режим XY-проекции STL: для реальной детали использовать только после диагностики.")
    if max(wire_bottom, wire_top) > settings.wire_max_mm_s:
        warn.append("Расчётная подача проволоки выше вашей контрольной границы. Это не ошибка, но нужен сухой прогон подачи.")
    if settings.layer_height > 0.75:
        warn.append("Z-шаг крупный: выше риск грубой поверхности и нестабильного формирования слоя.")
    if settings.hatch_spacing > 4.0:
        warn.append("Шаг дорожек крупный: возможны борозды и неперекрытие дорожек.")
    if max(settings.feed_bottom_mm_min, settings.feed_top_mm_min) > 1200.0:
        warn.append("Скорость движения выше 1200 мм/мин: выше риск холодной ванны и тыкания проволоки.")
    if time_plan.get("enabled") and time_plan.get("severity") == "bad":
        warn.append("Целевое время сильно расходится с расчётом: режим нужно проверять только коротким TEST.")
    if src_type == "STL 3D" and not summary.get("is_watertight", True):
        warn.append("STL не замкнутый: сечения могут быть неполными.")
    if n_layers > 300:
        info.append("Слоёв много: полный файл может генерироваться долго, сначала лучше TEST.")

    if critical:
        return "bad", "❌ Нельзя сразу запускать", critical + warn + info
    if warn:
        return "warn", "⚠️ Только TEST и сухой прогон", warn + info
    return "ok", "✅ Можно готовить TEST", ["Критичных расчётных ограничений не найдено. Всё равно нужен просмотрщик, аудит и сухой прогон."] + info


def _fmt_hm(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    h = int(seconds // 3600)
    m = int(round((seconds - h * 3600) / 60.0))
    if m >= 60:
        h += 1
        m -= 60
    return f"{h} ч {m:02d} мин"


def _estimate_time_before_generation(summary, settings: ProcessSettings):
    """Fast strategy-aware timing estimate before full G-code generation.

    Active deposition time is mainly metal volume / deposition rate, so several
    strategies can have similar active time. Total time must also include starts,
    Z approaches, W retracts and link/servo overhead; these terms depend strongly
    on trajectory strategy and were the reason old estimates looked unchanged.
    """
    height = max(float(summary.get("size_z", 0.0) or 0.0), 1e-9)
    n_layers = int(math.ceil(height / max(settings.layer_height, 1e-9)))
    area_wire = settings.wire_area_mm2()
    wire_bottom = _wire_for_feed(settings, settings.feed_bottom_mm_min, 0.0)
    wire_top = _wire_for_feed(settings, settings.feed_top_mm_min, 1.0)
    avg_wire = max(1e-9, (wire_bottom + wire_top) / 2.0)
    volume = float(summary.get("volume_mm3", float("nan")))
    if not math.isfinite(volume) or volume <= 0:
        volume = float(summary.get("size_x", 0.0)) * float(summary.get("size_y", 0.0)) * height * 0.20
    metal_rate_mm3_s = area_wire * avg_wire * max(settings.deposition_efficiency, 1e-9)
    active_s = volume / max(metal_rate_mm3_s, 1e-9)
    pause_s = n_layers * (settings.layer_pause_bottom_s + settings.layer_pause_top_s) / 2.0

    size_x = float(summary.get("size_x", 0.0) or 0.0)
    size_y = float(summary.get("size_y", 0.0) or 0.0)
    path_strategy = str(getattr(settings, "rotational_path_strategy", "hatch") or "hatch").strip().lower()
    radial_step = float(getattr(settings, "rotational_radial_step_mm", 0.0) or 0.0) or float(settings.hatch_spacing)
    radial_step = max(radial_step, 0.1)
    points_per_circle = max(16, int(getattr(settings, "rotational_points_per_circle", 160) or 160))
    c_like = path_strategy in ("rotary_c", "rotary_c_rings", "c_rings", "c_table", "stl_rotary_c_rings", "mesh_rotary_c_rings", "generic_rotary_c_rings")
    rings_like = path_strategy in ("rings", "xy_rings", "stl_xy_rings", "mesh_xy_rings")
    spiral_like = path_strategy in ("spiral", "xy_spiral", "stl_xy_spiral", "mesh_xy_spiral")

    z_descent_speed_mm_s = max(settings.work_z_feed_mm_min / 60.0, 1e-9)
    z_ascent_speed_mm_s = max(settings.rapid_feed_z_mm_min / 60.0, 1e-9)
    z_pair_s = settings.z_hop_mm / z_descent_speed_mm_s + settings.z_hop_mm / z_ascent_speed_mm_s
    restrike_s = settings.beam_preheat_s + settings.wire_settle_s + settings.beam_off_pause_s
    if settings.use_w_retract and settings.w_retract_mm > 0 and settings.w_retract_feed_mm_min > 0:
        restrike_s += 2.0 * settings.w_retract_mm / (settings.w_retract_feed_mm_min / 60.0)
    elif settings.use_m68_speed_retract:
        restrike_s += settings.speed_retract_time_s
    link_s = settings.wire_settle_s + 0.05

    if c_like or rings_like or spiral_like:
        equiv_area = max(volume / max(height, 1e-9), 1.0)
        equiv_radius = max(math.sqrt(equiv_area / math.pi), 0.5 * min(size_x, size_y, max(size_x, size_y)))
        radial_width = max(0.5 * min(size_x, size_y, max(size_x, size_y)), equiv_radius)
        passes_per_layer = max(1.0, radial_width / radial_step)
        if c_like:
            if str(getattr(settings, "rotary_c_motion_mode", "separate_rings")).strip().lower() == "no_pause_flat_rings":
                # No-pause C: beam/wire stay on, but the generator still deposits MANY
                # concentric rings per layer (radial fill from inner to outer radius),
                # NOT one ring per layer. The old estimate modelled a single ring at
                # radius_est and ignored the C speed cap, under-counting time ~19x on a
                # flange (real 4118 rings vs estimated 219). We now sum real ring time
                # over the concentric radii, applying the C-axis speed limit per radius.
                # Radial extent for the concentric-ring fill. Use the part's actual
                # outer radius (bounding box) rather than the volume-equivalent radius,
                # which underestimates a flat disc's radius and thus the ring count.
                r_outer = max(0.5 * max(size_x, size_y), radial_width, radial_step)
                r_inner = max(0.0, float(getattr(settings, "rotary_c_min_radius_mm", 0.0) or 0.0))
                if r_inner >= r_outer:
                    r_inner = 0.0
                rings_per_layer = max(1, int(math.ceil((r_outer - r_inner) / max(radial_step, 1e-9))))
                avg_f = max((settings.feed_bottom_mm_min + settings.feed_top_mm_min) * 0.5, 1e-9)
                c_max = float(getattr(settings, "rotary_c_max_deg_min", 2100.0) or 2100.0)
                c_limit_on = bool(getattr(settings, "rotary_c_auto_limit_feed", True))
                transition_deg = max(0.0, float(getattr(settings, "rotary_c_transition_angle_deg", 17.0) or 0.0))
                # Sum ring time across the concentric radii of one layer, then x layers.
                one_layer_s = 0.0
                one_layer_path = 0.0
                for k in range(rings_per_layer):
                    r_k = r_inner + (k + 0.5) * radial_step
                    if r_k <= 1e-6:
                        continue
                    circ = 2.0 * math.pi * r_k
                    # linear feed actually usable at this radius under the C cap
                    if bool(getattr(settings, "rotary_c_constant_velocity", False)):
                        # v4.2.9.31: mirror the generator's CV kinematics exactly,
                        # so the pre-generation estimate matches the real G-code.
                        from ebam_gcode_studio.core import _rotary_c_ring_kinematics as _cvk
                        _c_e, _f_e, _pf_e = _cvk(settings, r_k, avg_f)
                        v_lin = _f_e / 60.0
                    else:
                        v_lin = avg_f / 60.0  # mm/s requested
                        if c_limit_on:
                            c_req = 180.0 * avg_f / max(math.pi * r_k, 1e-9)  # deg/min needed
                            if c_req > c_max:
                                v_lin = (c_max * math.pi * r_k / 180.0) / 60.0  # clamped mm/s
                    one_layer_path += circ + (transition_deg / 360.0) * circ
                    one_layer_s += (circ + (transition_deg / 360.0) * circ) / max(v_lin, 1e-9)
                active_s = one_layer_s * n_layers
                if bool(getattr(settings, "thermal_min_layer_cycle_enabled", False)):
                    _cyc_s = float(getattr(settings, "thermal_min_layer_cycle_min", 3.0)) * 60.0
                    if one_layer_s < _cyc_s:
                        pause_s += max(_cyc_s - one_layer_s, float(getattr(settings, "thermal_min_dwell_s", 0.0))) * n_layers
                path_len = one_layer_path * n_layers
                pause_s = 0.0 if bool(getattr(settings, "rotary_c_disable_layer_pauses", False)) else pause_s
                physical_passes = float(rings_per_layer * n_layers)
                link_moves = max(0.0, float(rings_per_layer * n_layers - 1))
                strategy_note = (f"C без остановки: {rings_per_layer} концентрических колец/слой x {n_layers} слоёв "
                                 f"= {rings_per_layer * n_layers} колец; скорость каждого кольца ограничена столом C")
            else:
                physical_passes = n_layers * passes_per_layer
                link_moves = 0.0
                strategy_note = "поворотный стол C: много отдельных C360-колец, поэтому добавлены старты/Z/W на каждое кольцо"
                # Small-radius C speed limiting can increase active time. Approximate by checking inner radius.
                min_r = max(radial_step * 0.5, float(getattr(settings, "rotary_c_min_radius_mm", 18.0)))
                c_req = 180.0 * ((settings.feed_bottom_mm_min + settings.feed_top_mm_min) * 0.5) / max(math.pi * min_r, 1e-9)
                if bool(getattr(settings, "rotary_c_auto_limit_feed", True)) and c_req > float(getattr(settings, "rotary_c_max_deg_min", 2100.0)):
                    active_s *= min(c_req / max(float(getattr(settings, "rotary_c_max_deg_min", 2100.0)), 1e-9), 3.0)
        elif spiral_like:
            physical_passes = n_layers
            link_moves = 0.0
            active_s *= 0.97
            strategy_note = "XY-спираль: один непрерывный проход на слой, минимум стартов/остановов"
        else:
            physical_passes = n_layers
            link_moves = n_layers * passes_per_layer * points_per_circle * 0.12
            strategy_note = "концентрические XY-кольца: один проход на слой, но много коротких дуговых сегментов"
    else:
        axis_y = settings.direction.upper().startswith("Y")
        perp = size_x if axis_y else size_y
        seg_per_layer = max(1.0, perp / max(settings.hatch_spacing, 1e-9))
        if str(getattr(settings, "deposition_strategy", "continuous")).strip().lower() == "continuous":
            physical_passes = n_layers
            link_moves = n_layers * seg_per_layer
            strategy_note = "непрерывная змейка: один старт на слой и перемычки между соседними дорожками"
        else:
            physical_passes = n_layers * seg_per_layer
            link_moves = 0.0
            strategy_note = "параллельные посегментные проходы: каждый проход стартует отдельно"

    aux_s = physical_passes * (z_pair_s + restrike_s) + link_moves * link_s
    if c_like and str(getattr(settings, "rotary_c_motion_mode", "separate_rings")).strip().lower() == "no_pause_flat_rings":
        aux_s = 0.0 if (bool(getattr(settings, "rotary_c_disable_w_retract", False)) and bool(getattr(settings, "rotary_c_disable_z_hop", False))) else aux_s
        total_s = (active_s + pause_s + aux_s) * 1.03
    else:
        total_s = (active_s + pause_s + aux_s) * 1.12
    return {
        "n_layers": n_layers,
        "wire_bottom": wire_bottom,
        "wire_top": wire_top,
        "active_s": active_s,
        "pause_s": pause_s,
        "aux_s": aux_s,
        "est_segments": physical_passes + link_moves,
        "strategy_passes": physical_passes,
        "strategy_link_moves": link_moves,
        "strategy_note": strategy_note,
        "total_s": total_s,
        "volume_used_mm3": volume,
        "is_approximate": True,
    }


def _clamp_float(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _current_limited_feed_cap(settings: ProcessSettings) -> tuple[float, float]:
    """Maximum feed that still preserves requested J/mm without current clipping.

    In current-setpoint mode E0 is the primary user input, so increasing F is
    allowed up to the software feed guard; actual J/mm will fall and be shown in
    the table instead of being hidden by an energy cap.
    """
    if str(getattr(settings, "beam_current_mode", "energy")).strip().lower() in ("current", "manual_current", "e0", "fixed_current"):
        return 3000.0, 3000.0
    cap0 = settings.current_max_ma * 60.0 * settings.voltage_kv / max(settings.target_energy_bottom_j_per_mm, 1e-9)
    cap1 = settings.current_max_ma * 60.0 * settings.voltage_kv / max(settings.target_energy_top_j_per_mm, 1e-9)
    return min(3000.0, cap0), min(3000.0, cap1)



def _display_current_energy_pair(settings: ProcessSettings) -> dict:
    """Calculate bottom/top E0 and J/mm exactly as the generator does for UI tables."""
    def one(feed, energy, set_current):
        travel = max(float(feed) / 60.0, 1e-9)
        mode = str(getattr(settings, "beam_current_mode", "energy") or "energy").strip().lower()
        if mode in ("current", "manual_current", "e0", "fixed_current"):
            needed = float(set_current)
            target_j = float(settings.voltage_kv) * needed / travel
        else:
            target_j = float(energy)
            needed = target_j * float(feed) / max(60.0 * float(settings.voltage_kv), 1e-9)
        current = max(float(settings.current_min_ma), min(float(settings.current_max_ma), needed))
        actual_j = float(settings.voltage_kv) * current / travel
        return needed, current, target_j, actual_j
    b = one(settings.feed_bottom_mm_min, settings.target_energy_bottom_j_per_mm, getattr(settings, "beam_current_bottom_ma", 0.0))
    u = one(settings.feed_top_mm_min, settings.target_energy_top_j_per_mm, getattr(settings, "beam_current_top_ma", 0.0))
    return {
        "current_bottom_need": b[0], "current_bottom": b[1], "target_j_bottom": b[2], "actual_j_bottom": b[3],
        "current_top_need": u[0], "current_top": u[1], "target_j_top": u[2], "actual_j_top": u[3],
        "mode": str(getattr(settings, "beam_current_mode", "energy") or "energy"),
    }

def _wire_for_feed(settings: ProcessSettings, feed_mm_min: float, ratio: float = 0.0) -> float:
    """Wire feed E2 from the same rule used by core.layer_parameters().

    v4.2.9.13 supports operator wire mode:
    - auto: recalculate E2 from F, Z-step and hatch/radial step;
    - manual_constant: use one fixed E2;
    - manual_bottom_top: interpolate E2 over height.
    """
    mode = str(getattr(settings, "wire_feed_mode", "auto") or "auto").strip().lower()
    if mode == "manual_constant":
        q = float(getattr(settings, "wire_feed_manual_mm_s", 0.0) or 0.0)
        if q > 0:
            return q
    if mode == "manual_bottom_top":
        qb = float(getattr(settings, "wire_feed_bottom_mm_s", 0.0) or 0.0)
        qt = float(getattr(settings, "wire_feed_top_mm_s", 0.0) or 0.0)
        if max(qb, qt) > 0:
            if qb <= 0:
                qb = qt
            if qt <= 0:
                qt = qb
            r = min(max(float(ratio), 0.0), 1.0)
            return max(0.0, qb + (qt - qb) * r)
    area = max(settings.wire_area_mm2() * float(settings.deposition_efficiency), 1e-9)
    return max(0.0, float(settings.layer_height) * (float(feed_mm_min) / 60.0) * float(settings.hatch_spacing) / area)


def _build_calc_rows(settings: ProcessSettings, n_layers: int, area_wire: float,
                     recalc: dict, cinfo: dict | None, eta: dict,
                     fmt_hm, radial_step_fn) -> list:
    """Contextual summary rows: only parameters that are actually in effect for
    the current source/strategy/mode are shown. C-axis rows appear only when a
    C-limit context exists; the C+Z transition row only in no-pause mode; limiter
    and clipping rows only when they actually changed something; contour /
    strategy-overhead / target-vs-actual rows only when non-trivial. This removes
    the confusing dashes-for-unused-modes from the old always-33-rows table."""
    rows: list = []
    add = lambda k, v: rows.append((k, str(v)))

    strategy = str(getattr(settings, "rotational_path_strategy", "hatch")).lower()
    is_rot_step = ("rotary" in strategy) or strategy in ("rings", "spiral")
    no_pause = str(getattr(settings, "rotary_c_motion_mode", "separate_rings")).lower() == "no_pause_flat_rings"
    step_mm = float(radial_step_fn(settings)) if is_rot_step else float(settings.hatch_spacing)

    cb_need = float(recalc["effective_current_bottom_need"]); ct_need = float(recalc["effective_current_top_need"])
    cb = float(recalc["effective_current_bottom"]); ct = float(recalc["effective_current_top"])
    wb = float(recalc["effective_wire_bottom"]); wt = float(recalc["effective_wire_top"])
    aj_b = float(recalc["effective_actual_energy_bottom"]); aj_t = float(recalc["effective_actual_energy_top"])
    tj_b = float(recalc["effective_energy_bottom"]); tj_t = float(recalc["effective_energy_top"])
    denom = max(settings.layer_height * step_mm, 1e-9)

    add("Слоёв", n_layers)

    limiter = str(recalc.get("limiter_reason") or "нет")
    feeds_changed = (abs(float(recalc["requested_feed_bottom"]) - float(recalc["effective_feed_bottom"])) > 1e-6 or
                     abs(float(recalc["requested_feed_top"]) - float(recalc["effective_feed_top"])) > 1e-6)
    if limiter != "нет" or feeds_changed:
        add("Ограничитель F", limiter)
        add("F заданная низ/верх, мм/мин", f"{recalc['requested_feed_bottom']:.1f} / {recalc['requested_feed_top']:.1f}")
        add("F фактическая низ/верх, мм/мин", f"{recalc['effective_feed_bottom']:.1f} / {recalc['effective_feed_top']:.1f}")
    else:
        add("Скорость низ/верх, мм/мин", f"{recalc['effective_feed_bottom']:.1f} / {recalc['effective_feed_top']:.1f}")

    if cinfo:
        add("C требуется низ/верх, град/мин", f"{cinfo.get('required_c_bottom', 0.0):.1f} / {cinfo.get('required_c_top', 0.0):.1f}")
        add("C разрешено, град/мин", f"{cinfo.get('cmax_deg_min', 0.0):.1f}")
        add("Радиус для C-лимита, мм", f"{cinfo.get('radius_mm', 0.0):.2f} ({cinfo.get('radius_source', '—')})")
        if cinfo.get("real_radius_mm") is not None:
            add("Реальный Rmin траектории, мм", f"{cinfo.get('real_radius_mm'):.2f}")
        if cinfo.get("radius_below_warning"):
            add("Порог предупреждения малого радиуса, мм", f"{cinfo.get('warning_radius_mm', 0.0):.2f}")
        add("Режим C-колец", "C без остановки, фиксированные Z-кольца" if no_pause else "обычные отдельные кольца/траектория")
        if no_pause:
            add("Переход C+Z, град", f"{float(getattr(settings, 'rotary_c_transition_angle_deg', 0.0)):.1f}")

    wire_mode = str(getattr(settings, "wire_feed_mode", "auto"))
    add("Режим подачи E2", {"auto": "авто по F/Z/шагу", "manual_constant": "ручная постоянная",
                            "manual_bottom_top": "ручная низ/верх"}.get(wire_mode, wire_mode))
    add("Режим расчёта E0", "уставка тока E0" if str(getattr(settings, "beam_current_mode", "energy")).lower() != "energy" else "энергия Дж/мм")

    if abs(float(recalc["requested_current_bottom_need"]) - cb_need) > 1e-3 or abs(float(recalc["requested_current_top_need"]) - ct_need) > 1e-3:
        add("Ток до ограничителя низ/верх, мА", f"{recalc['requested_current_bottom_need']:.3f} / {recalc['requested_current_top_need']:.3f}")
    clipped = (abs(cb - cb_need) > 1e-3) or (abs(ct - ct_need) > 1e-3)
    if clipped:
        add("Ток требуемый низ/верх, мА", f"{cb_need:.3f} / {ct_need:.3f}")
        add("Лимиты E0 (нижний/верхний), мА", f"{settings.current_min_ma:.3f} / {settings.current_max_ma:.3f}")
    add("Ток в G-code низ/верх, мА", f"{cb:.3f} / {ct:.3f}")
    if settings.current_low_warning_ma > 0 and min(cb, ct) < settings.current_low_warning_ma:
        add("Порог предупреждения малого E0, мА", round(settings.current_low_warning_ma, 3))

    if abs(tj_b - aj_b) > 0.05 or abs(tj_t - aj_t) > 0.05:
        add("Энергия цель низ/верх, Дж/мм", f"{tj_b:.1f} / {tj_t:.1f}")
    add("Энергия факт низ/верх, Дж/мм", f"{aj_b:.1f} / {aj_t:.1f}")
    add("Шаг заполнения, мм", f"{step_mm:.3f} ({'радиальный (кольца/спираль)' if is_rot_step else 'hatch'})")
    _lay_now = float(getattr(settings, "layer_height", 0.0) or 0.0)
    if _lay_now > FIELD_PROVEN_MAX_LAYER_MM + 1e-9:
        add("Высота слоя вне проверенного диапазона, мм",
            f"{_lay_now:.2f}  ⚠ на этой машине проверено до {FIELD_PROVEN_MAX_LAYER_MM:.2f} мм — только через TEST")
    _qv_b, _qv_t = aj_b / denom, aj_t / denom
    _qv_worst = max(_qv_b, _qv_t) if max(_qv_b, _qv_t) > 90.0 else min(_qv_b, _qv_t)
    if max(_qv_b, _qv_t) >= 110.0:
        _qv_mark = "  ⚠ ПЕРЕГРЕВ (норма 55–90)"
    elif min(_qv_b, _qv_t) <= 42.0:
        _qv_mark = "  ⚠ НЕ ПРОПЛАВИТ (норма 55–90)"
    elif max(_qv_b, _qv_t) > 90.0 or min(_qv_b, _qv_t) < 55.0:
        _qv_mark = "  · вне полосы 55–90"
    else:
        _qv_mark = "  — в норме (55–90)"
    add("Энергия объёма факт низ/верх, Дж/мм³", f"{_qv_b:.1f} / {_qv_t:.1f}{_qv_mark}")

    floor_w = float(getattr(settings, "min_beam_power_w", 0.0) or 0.0)
    if bool(getattr(settings, "power_floor_warning_enabled", True)) and floor_w > 0:
        p_min = settings.voltage_kv * min(cb, ct); p_max = settings.voltage_kv * max(cb, ct)
        mark = " ⚠ НИЖЕ ПОРОГА" if p_min < floor_w - 1e-9 else " — ок"
        add("Мощность пучка низ/верх, Вт", f"{settings.voltage_kv * cb:.0f} / {settings.voltage_kv * ct:.0f} (порог {floor_w:.0f}{mark})")

    if abs(float(recalc["requested_wire_bottom"]) - wb) > 1e-3 or abs(float(recalc["requested_wire_top"]) - wt) > 1e-3:
        add("Проволока до ограничителя низ/верх, мм/с", f"{recalc['requested_wire_bottom']:.3f} / {recalc['requested_wire_top']:.3f}")
    add("Проволока факт низ/верх, мм/с", f"{wb:.3f} / {wt:.3f}")
    if max(wb, wt) > 0.85 * settings.wire_max_mm_s:
        add("Контроль подачи, мм/с", f"{settings.wire_min_mm_s:.1f}...{settings.wire_max_mm_s:.1f}")
    add("Площадь проволоки, мм²", round(area_wire, 3))

    if int(settings.contour_passes) > 0:
        add("Контурных проходов", settings.contour_passes)
    passes = float(eta.get("strategy_passes", eta.get("est_segments", 0.0)) or 0.0)
    links = float(eta.get("strategy_link_moves", 0.0) or 0.0)
    if passes > 0:
        add("Стартов/проходов стратегии, прибл.", round(passes, 1))
    if links > 0:
        add("Перемычек/сегментных переходов, прибл.", round(links, 1))

    add("Время активной наплавки, прибл.", fmt_hm(eta["active_s"]))
    if float(eta.get("aux_s", 0.0) or 0.0) > 0.5:
        add("Доп. время стратегии, прибл.", fmt_hm(eta.get("aux_s", 0.0)))
    add("Полное время с паузами, прибл.", fmt_hm(eta["total_s"]))
    return rows


def _process_recalc_snapshot(settings: ProcessSettings, c_limit_info: dict | None = None) -> dict:
    """One visible recalculation chain for the UI.

    Requested values are the operator setpoints before C limiting; effective values
    are the values actually used for downstream E0/E2/J/mm/time calculations.
    This makes every changed parameter visible instead of hiding C-limit effects in
    only the time estimate.
    """
    cinfo = c_limit_info or {}
    req_b = float(cinfo.get("requested_bottom", cinfo.get("old_bottom", settings.feed_bottom_mm_min)))
    req_t = float(cinfo.get("requested_top", cinfo.get("old_top", settings.feed_top_mm_min)))
    eff_b = float(cinfo.get("effective_bottom", cinfo.get("new_bottom", settings.feed_bottom_mm_min)))
    eff_t = float(cinfo.get("effective_top", cinfo.get("new_top", settings.feed_top_mm_min)))

    requested_settings = replace(settings, feed_bottom_mm_min=req_b, feed_top_mm_min=req_t)
    effective_settings = replace(settings, feed_bottom_mm_min=eff_b, feed_top_mm_min=eff_t)
    req_ce = _display_current_energy_pair(requested_settings)
    eff_ce = _display_current_energy_pair(effective_settings)

    return {
        "requested_feed_bottom": req_b,
        "requested_feed_top": req_t,
        "effective_feed_bottom": eff_b,
        "effective_feed_top": eff_t,
        "requested_wire_bottom": _wire_for_feed(requested_settings, req_b, 0.0),
        "requested_wire_top": _wire_for_feed(requested_settings, req_t, 1.0),
        "effective_wire_bottom": _wire_for_feed(effective_settings, eff_b, 0.0),
        "effective_wire_top": _wire_for_feed(effective_settings, eff_t, 1.0),
        "requested_current_bottom_need": req_ce["current_bottom_need"],
        "requested_current_top_need": req_ce["current_top_need"],
        "effective_current_bottom_need": eff_ce["current_bottom_need"],
        "effective_current_top_need": eff_ce["current_top_need"],
        "effective_current_bottom": eff_ce["current_bottom"],
        "effective_current_top": eff_ce["current_top"],
        "requested_energy_bottom": req_ce["target_j_bottom"],
        "requested_energy_top": req_ce["target_j_top"],
        "effective_energy_bottom": eff_ce["target_j_bottom"],
        "effective_energy_top": eff_ce["target_j_top"],
        "effective_actual_energy_bottom": eff_ce["actual_j_bottom"],
        "effective_actual_energy_top": eff_ce["actual_j_top"],
        "limiter_reason": (cinfo or {}).get("reason", "нет"),
        "limited": bool((cinfo or {}).get("limited", False)),
    }





def _rotary_c_like_strategy(settings: ProcessSettings) -> bool:
    path_strategy = str(getattr(settings, "rotational_path_strategy", "") or "").strip().lower()
    return path_strategy in (
        "rotary_c", "rotary_c_rings", "c_rings", "c_table",
        "stl_rotary_c_rings", "mesh_rotary_c_rings", "generic_rotary_c_rings",
    )


def _rotary_c_radius_limit_info(summary: dict, settings: ProcessSettings) -> dict:
    """Choose the radius used for the UI-level C feed limiter.

    v4.2.9.13 separates two meanings that were previously mixed:
    - rotary_c_min_radius_mm is only a warning threshold for small radii;
    - the C limiter uses the real/estimated minimum toolpath radius when geometry is known.

    For parametric rotational vessels we can estimate the actual first C-ring radius
    from outer diameter, wall thickness, bottom solid region and radial step. For
    STL/generic geometry before sectioning we may not know it yet, so we fall back
    to the warning radius and mark the source explicitly. The generator still checks
    every real C-ring as a second safety layer.
    """
    try:
        warning_r = float(getattr(settings, "rotary_c_min_radius_mm", 18.0) or 18.0)
    except Exception:
        warning_r = 18.0
    warning_r = max(0.1, warning_r)
    radial_step = float(getattr(settings, "rotational_radial_step_mm", 0.0) or 0.0) or float(getattr(settings, "hatch_spacing", 2.0) or 2.0)
    radial_step = max(radial_step, 0.1)

    real_r = None
    source = "fallback_warning_radius"
    note = "real radius is unknown before generation; warning radius is used as a conservative fallback"

    for key in ("rotary_c_min_radius_used_mm", "real_min_rotary_radius_mm"):
        try:
            v = float(summary.get(key, 0.0) or 0.0)
            if v > 0:
                real_r = v
                source = key
                note = "using measured/generated minimum C-ring radius"
                break
        except Exception:
            pass

    if real_r is None and str(summary.get("source_type", "")).strip().lower() == "rotational_vessel":
        try:
            outer_r = 0.5 * float(summary.get("min_outer_diameter_mm", 0.0) or 0.0)
            wall = max(float(summary.get("wall_thickness_mm", 0.0) or 0.0), 0.0)
            bottom_solid = max(float(summary.get("bottom_solid_mm", 0.0) or 0.0), 0.0)
            if outer_r > 0:
                if bottom_solid > 1e-9:
                    # Solid bottom/disc mode starts close to centre. This is a real geometric limit, not a warning threshold.
                    real_r = max(0.5 * radial_step, 0.1)
                    source = "real_toolpath_radius_bottom_solid"
                    note = "solid bottom uses small/centre rings, so C limit uses the first actual ring radius"
                elif wall > 0 and wall < outer_r:
                    inner_r = max(outer_r - wall, 0.0)
                    real_r = max(inner_r + 0.5 * radial_step, 0.1)
                    real_r = min(real_r, outer_r)
                    source = "real_toolpath_radius_wall"
                    note = "parametric vessel: C limit uses estimated inner-to-outer first ring radius"
                else:
                    real_r = max(0.5 * radial_step, 0.1)
                    source = "real_toolpath_radius_solid"
                    note = "solid/filled section: C limit uses first actual small-radius ring"
        except Exception:
            real_r = None

    if real_r is None:
        used_r = warning_r
    else:
        used_r = max(0.1, float(real_r))

    return {
        "real_radius_mm": None if real_r is None else float(real_r),
        "warning_radius_mm": float(warning_r),
        "used_radius_mm": float(used_r),
        "source": source,
        "note": note,
        "below_warning": bool(real_r is not None and float(real_r) < warning_r - 1e-9),
    }


def _estimate_min_rotary_radius_for_ui(summary: dict, settings: ProcessSettings) -> float:
    """Radius used for visible/global C feed limiting in UI tables before generation."""
    return float(_rotary_c_radius_limit_info(summary, settings)["used_radius_mm"])


def _rotary_c_limit_status(summary: dict, settings: ProcessSettings, c_limit_info: dict | None = None) -> dict | None:
    """Build a visible explanation of the C-axis speed limiter.

    This does not change the process. It only exposes the chain that was previously
    unclear to the operator: requested linear F -> required C deg/min -> allowed C
    deg/min -> effective linear F used for E0/E2/time.
    """
    if not _rotary_c_like_strategy(settings):
        return None
    cmax = max(float(getattr(settings, "rotary_c_max_deg_min", 2100.0) or 2100.0), 1e-9)
    radius_info = _rotary_c_radius_limit_info(summary, settings)
    radius_mm = float((c_limit_info or {}).get("radius_mm", radius_info["used_radius_mm"]))
    radius_mm = max(radius_mm, 0.1)
    auto_limit = bool(getattr(settings, "rotary_c_auto_limit_feed", True))

    requested_bottom = float((c_limit_info or {}).get("old_bottom", settings.feed_bottom_mm_min))
    requested_top = float((c_limit_info or {}).get("old_top", settings.feed_top_mm_min))
    effective_bottom = float((c_limit_info or {}).get("new_bottom", settings.feed_bottom_mm_min))
    effective_top = float((c_limit_info or {}).get("new_top", settings.feed_top_mm_min))
    feed_cap = float((c_limit_info or {}).get("feed_cap_mm_min", cmax * math.pi * radius_mm / 180.0))

    required_c_bottom = rotary_c_speed_deg_min(requested_bottom, radius_mm)
    required_c_top = rotary_c_speed_deg_min(requested_top, radius_mm)
    effective_c_bottom = rotary_c_speed_deg_min(effective_bottom, radius_mm)
    effective_c_top = rotary_c_speed_deg_min(effective_top, radius_mm)
    limited = bool(auto_limit and (effective_bottom < requested_bottom - 1e-6 or effective_top < requested_top - 1e-6))
    if limited:
        reason = "лимит C-оси"
        operator_message = (
            "F ограничена поворотным столом C. Проволока E2, ток E0, энергия и время "
            "пересчитаны по фактической скорости F, а не по запрошенной."
        )
    elif auto_limit:
        reason = "нет ограничения"
        operator_message = "C-ось успевает выполнить запрошенную скорость; F не ограничена."
    else:
        reason = "автоограничение выключено"
        operator_message = (
            "Автоограничение F по C выключено. Расчёт идёт по заданной F; перед реальным запуском "
            "проверьте, что требуемая скорость C не выше безопасного лимита."
        )
    return {
        "enabled": auto_limit,
        "limited": limited,
        "reason": reason,
        "radius_mm": radius_mm,
        "real_radius_mm": (c_limit_info or {}).get("real_radius_mm", radius_info.get("real_radius_mm")),
        "warning_radius_mm": (c_limit_info or {}).get("warning_radius_mm", radius_info.get("warning_radius_mm")),
        "radius_source": (c_limit_info or {}).get("radius_source", radius_info.get("source")),
        "radius_note": (c_limit_info or {}).get("radius_note", radius_info.get("note")),
        "radius_below_warning": bool((c_limit_info or {}).get("radius_below_warning", radius_info.get("below_warning", False))),
        "cmax_deg_min": cmax,
        "feed_cap_mm_min": feed_cap,
        "requested_bottom": requested_bottom,
        "requested_top": requested_top,
        "effective_bottom": effective_bottom,
        "effective_top": effective_top,
        "required_c_bottom": required_c_bottom,
        "required_c_top": required_c_top,
        "effective_c_bottom": effective_c_bottom,
        "effective_c_top": effective_c_top,
        "operator_message": operator_message,
    }


def _apply_rotary_c_feed_limit_to_settings(summary: dict, settings: ProcessSettings):
    """Apply C-axis speed cap to visible/global F when rotary C auto-limit is enabled.

    v4.2.9.13 applies the C limit before the calculation table and before G-code generation.
    Therefore every downstream value is recalculated from effective F: E0, E2,
    J/mm, J/mm3, time estimate and warnings. The generator still keeps per-ring
    protection as a second safety layer.
    """
    if not _rotary_c_like_strategy(settings):
        return settings, None
    if not bool(getattr(settings, "rotary_c_auto_limit_feed", True)):
        return settings, _rotary_c_limit_status(summary, settings, None)
    if bool(getattr(settings, "rotary_c_constant_velocity", False)):
        # v4.2.9.31: CV mode computes speed per ring itself; the GLOBAL clamp by the
        # fallback radius (18 mm) crushed F to ~188 mm/min and poisoned E2/energy/
        # time in the summary and in separate-ring generators. Skip the clamp.
        st = _rotary_c_limit_status(summary, settings, None)
        if isinstance(st, dict):
            st["cv_mode"] = True
            st["note"] = "Компенсация по радиусу активна: скорость и E2 считаются по-кольцево, глобальный зажим F не применяется."
        return settings, st
    cmax = max(float(getattr(settings, "rotary_c_max_deg_min", 2100.0) or 2100.0), 1e-9)
    radius_info = _rotary_c_radius_limit_info(summary, settings)
    r = float(radius_info["used_radius_mm"])
    feed_cap = cmax * math.pi * r / 180.0
    old_b = float(settings.feed_bottom_mm_min)
    old_t = float(settings.feed_top_mm_min)
    new_b = min(old_b, feed_cap)
    new_t = min(old_t, feed_cap)
    info = {
        "radius_mm": r,
        "real_radius_mm": radius_info.get("real_radius_mm"),
        "warning_radius_mm": radius_info.get("warning_radius_mm"),
        "radius_source": radius_info.get("source"),
        "radius_note": radius_info.get("note"),
        "radius_below_warning": radius_info.get("below_warning", False),
        "cmax_deg_min": cmax,
        "feed_cap_mm_min": feed_cap,
        "old_bottom": old_b,
        "old_top": old_t,
        "new_bottom": new_b,
        "new_top": new_t,
    }
    new_settings = replace(settings, feed_bottom_mm_min=float(new_b), feed_top_mm_min=float(new_t))
    return new_settings, _rotary_c_limit_status(summary, new_settings, info)

def _fit_settings_to_target_time(summary, base: ProcessSettings, target_s: float, fit_mode: str, levers: dict | None = None):
    """Return settings adjusted toward target total time and a human-readable plan.

    v4.2 logic:
    - feed_only: changes only F.
    - feed_layer_hatch: changes F, Z-step and hatch spacing.
    - full_process: may also change thermal pauses and target J/mm within
      conservative boundaries. It NEVER raises current_max_ma automatically:
      current_max_ma is a user-selected equipment/process limit.

    v4.2.9.31: selectable levers. `levers` is a dict describing which parameters
    the operator ALLOWS the tuner to change, each with an optional hard limit:
        {
          "layer_height":   {"enabled": bool, "max": mm},      # thicker -> fewer layers
          "radial_step":    {"enabled": bool, "max": mm},      # wider -> fewer rings
          "c_speed":        {"enabled": bool, "max": deg_min}, # faster table -> faster
          "current":        {"enabled": bool, "max": mA},      # caps fusible speed
        }
    E2 (wire) is ALWAYS dependent: it is recomputed from geometry x speed so the
    deposited volume stays correct (prevents the over-feed that ruined earlier
    parts). If a lever is disabled, the tuner keeps that parameter at its base
    value. If the allowed levers cannot reach the target, the plan says so
    honestly and reports the real achievable minimum/maximum time.

    Wire maximum remains a warning threshold, not a hard clamp, because the real
    feeder capability is user-defined by the user/operator.
    """
    target_s = float(target_s or 0.0)
    lv = levers or {}
    def _lever(name, default_enabled):
        d = lv.get(name, {}) if isinstance(lv, dict) else {}
        return bool(d.get("enabled", default_enabled)), d.get("max", None)
    # Which levers may the tuner move? Defaults preserve legacy behaviour per mode.
    full_process = ("full" in fit_mode) or ("all" in fit_mode) or ("process" in fit_mode)
    aggressive = full_process or ("layer" in fit_mode) or ("hatch" in fit_mode) or ("z" in fit_mode)
    allow_layer, cap_layer = _lever("layer_height", aggressive)
    allow_step, cap_step = _lever("radial_step", aggressive)
    allow_cspeed, cap_cspeed = _lever("c_speed", False)
    allow_current, cap_current = _lever("current", False)
    out = replace(base, target_total_time_s=max(0.0, target_s), target_time_mode=fit_mode)
    # Apply user hard caps to the starting point where given.
    if allow_current and cap_current is not None:
        out = replace(out, current_max_ma=float(cap_current))
    if allow_cspeed and cap_cspeed is not None:
        out = replace(out, rotary_c_max_deg_min=float(cap_cspeed))
    base_eta = _estimate_time_before_generation(summary, out)
    plan = {
        "enabled": target_s > 0.0,
        "target_s": target_s,
        "base_total_s": base_eta["total_s"],
        "adjusted_total_s": base_eta["total_s"],
        "possible": True,
        "severity": "ok",
        "messages": [],
        "base_settings": {
            "layer_height": base.layer_height,
            "hatch_spacing": base.hatch_spacing,
            "feed_bottom": base.feed_bottom_mm_min,
            "feed_top": base.feed_top_mm_min,
            "energy_bottom": base.target_energy_bottom_j_per_mm,
            "energy_top": base.target_energy_top_j_per_mm,
            "pause_bottom": base.layer_pause_bottom_s,
            "pause_top": base.layer_pause_top_s,
            "current_max": base.current_max_ma,
        },
        "applied_settings": {},
    }
    if target_s <= 0.0:
        return out, plan

    if target_s < 5 * 60:
        plan["messages"].append("Цель меньше 5 минут: для EBAM это почти всегда только диагностический фрагмент, а не полноценная деталь.")

    # Allowed technological range for automatic fitting. These are not hard machine limits,
    # but reasonable software guard rails so the app does not silently create absurd regimes.
    feed_min = 10.0
    feed_hard_max = 3000.0
    layer_min = 0.08
    hatch_min = 0.30
    layer_max_quality, hatch_max_quality = 0.60, 3.00
    layer_max_extreme, hatch_max_extreme = 0.90, 4.50
    if full_process:
        # Experimental wider range. The UI will warn about quality loss.
        layer_max_extreme = 1.20
        hatch_max_extreme = 6.00
    # v4.2.9.31: honour user hard caps on the geometric levers. A disabled lever is
    # pinned to its base value (min==max==base) so the loop cannot move it.
    if allow_layer:
        if cap_layer is not None:
            layer_max_extreme = min(layer_max_extreme, float(cap_layer))
            layer_max_quality = min(layer_max_quality, float(cap_layer))
    else:
        layer_min = layer_max_quality = layer_max_extreme = float(base.layer_height)
    if allow_step:
        if cap_step is not None:
            hatch_max_extreme = min(hatch_max_extreme, float(cap_step))
            hatch_max_quality = min(hatch_max_quality, float(cap_step))
    else:
        hatch_min = hatch_max_quality = hatch_max_extreme = float(base.hatch_spacing)
    # geometry levers actually usable this run
    geo_enabled = allow_layer or allow_step

    # Energy range used only by full_process. Reducing J/mm is a last resort to
    # let F rise under the selected current limit; increasing it does not help time,
    # therefore we only adjust it downward for too-short targets.
    energy_bottom_start = max(base.target_energy_bottom_j_per_mm, 1e-9)
    energy_top_start = max(base.target_energy_top_j_per_mm, 1e-9)
    energy_min_factor = 0.60

    for _ in range(18):
        eta = _estimate_time_before_generation(summary, out)
        current_s = max(eta["total_s"], 1.0)
        ratio = current_s / max(target_s, 1.0)  # >1 means we need faster deposition
        if 0.96 <= ratio <= 1.04:
            break

        if ratio > 1.0:
            # Need shorter time.
            if full_process:
                # First remove artificial delay. This is less destructive than reducing energy,
                # but too little pause may overheat the part.
                pause_factor = max(0.70, 1.0 / min(ratio, 1.30))
                out.layer_pause_bottom_s = max(0.0, out.layer_pause_bottom_s * pause_factor)
                out.layer_pause_top_s = max(0.0, out.layer_pause_top_s * pause_factor)

            # Speed up feed while respecting the USER-SET current limit.
            fcap0, fcap1 = _current_limited_feed_cap(out)
            allowed0 = max(feed_min, min(feed_hard_max, fcap0))
            allowed1 = max(feed_min, min(feed_hard_max, fcap1))
            feed_step = min(ratio, 1.35)
            new_f0 = min(out.feed_bottom_mm_min * feed_step, allowed0)
            new_f1 = min(out.feed_top_mm_min * feed_step, allowed1)
            feed_changed = (new_f0 > out.feed_bottom_mm_min + 1e-6) or (new_f1 > out.feed_top_mm_min + 1e-6)
            out.feed_bottom_mm_min = new_f0
            out.feed_top_mm_min = new_f1

            if full_process and not feed_changed:
                # If feed is blocked by current limit, allow lower energy as an explicit
                # quality-risk tradeoff. This keeps current within the selected limit.
                new_e0 = max(energy_bottom_start * energy_min_factor, out.target_energy_bottom_j_per_mm / min(ratio, 1.25))
                new_e1 = max(energy_top_start * energy_min_factor, out.target_energy_top_j_per_mm / min(ratio, 1.25))
                energy_changed = (new_e0 < out.target_energy_bottom_j_per_mm - 1e-6) or (new_e1 < out.target_energy_top_j_per_mm - 1e-6)
                out.target_energy_bottom_j_per_mm = new_e0
                out.target_energy_top_j_per_mm = new_e1
                if energy_changed:
                    # Recompute cap and try another feed increase immediately.
                    fcap0, fcap1 = _current_limited_feed_cap(out)
                    out.feed_bottom_mm_min = min(out.feed_bottom_mm_min * min(ratio, 1.20), max(feed_min, min(feed_hard_max, fcap0)))
                    out.feed_top_mm_min = min(out.feed_top_mm_min * min(ratio, 1.20), max(feed_min, min(feed_hard_max, fcap1)))

            if geo_enabled:
                # Increase bead productivity geometrically. This is what really reduces
                # layer count / path density, but it may worsen geometry and surface.
                # Each lever moves only if the operator enabled it.
                geom_step = min(math.sqrt(ratio), 1.18 if not full_process else 1.25)
                if allow_layer:
                    out.layer_height = min(out.layer_height * geom_step, layer_max_extreme)
                if allow_step:
                    out.hatch_spacing = min(out.hatch_spacing * geom_step, hatch_max_extreme)
        else:
            # Need longer time.
            slow_step = max(ratio, 0.65)
            out.feed_bottom_mm_min = max(out.feed_bottom_mm_min * slow_step, feed_min)
            out.feed_top_mm_min = max(out.feed_top_mm_min * slow_step, feed_min)
            if geo_enabled:
                geom_step = max(math.sqrt(max(ratio, 0.20)), 0.82 if not full_process else 0.75)
                if allow_layer:
                    out.layer_height = max(out.layer_height * geom_step, layer_min)
                if allow_step:
                    out.hatch_spacing = max(out.hatch_spacing * geom_step, hatch_min)
            if full_process:
                # If still too fast after reducing speed/geometry, add thermal pauses.
                eta2 = _estimate_time_before_generation(summary, out)
                if eta2["total_s"] < target_s * 0.96:
                    height = max(float(summary.get("size_z", 0.0) or 0.0), 1e-9)
                    n_layers = max(1, int(math.ceil(height / max(out.layer_height, 1e-9))))
                    missing_s = max(0.0, target_s - eta2["total_s"])
                    add_per_layer = min(8.0, missing_s / n_layers)
                    out.layer_pause_bottom_s = min(8.0, out.layer_pause_bottom_s + add_per_layer * 0.65)
                    out.layer_pause_top_s = min(12.0, out.layer_pause_top_s + add_per_layer)

    # For rotary C strategies the radial step is what the estimator/generator use,
    # so keep it in sync with the hatch lever when the step lever is enabled.
    if allow_step and _rotary_c_like_strategy(out):
        out = replace(out, rotational_radial_step_mm=float(out.hatch_spacing))

    adj_eta = _estimate_time_before_generation(summary, out)
    plan["adjusted_total_s"] = adj_eta["total_s"]
    err_pct = 100.0 * (adj_eta["total_s"] - target_s) / max(target_s, 1.0)
    plan["error_pct"] = err_pct
    plan["possible"] = abs(err_pct) <= 18.0
    plan["severity"] = "ok" if abs(err_pct) <= 10.0 else ("warn" if abs(err_pct) <= 25.0 else "bad")
    # v4.2.9.31: record which levers were active and the achievable bound, so the UI
    # can tell the operator exactly why a target could not be met and what to relax.
    plan["levers_active"] = {
        "layer_height": allow_layer, "radial_step": allow_step,
        "c_speed": allow_cspeed, "current": allow_current,
    }
    if not plan["possible"] and adj_eta["total_s"] > target_s:
        # Target too short for the ALLOWED levers. Name the disabled levers that would help.
        suggestions = []
        if not allow_layer:
            suggestions.append("высоту слоя")
        if not allow_step:
            suggestions.append("шаг колец")
        if not allow_cspeed:
            suggestions.append("скорость стола C")
        if not allow_current:
            suggestions.append("лимит тока пучка")
        plan["min_achievable_s"] = adj_eta["total_s"]
        if suggestions:
            plan["messages"].append(
                "В заданное время не уложиться разрешёнными параметрами. Минимально достижимо "
                f"≈ {adj_eta['total_s']/3600:.1f} ч. Чтобы ускорить дальше, разрешите менять: "
                + ", ".join(suggestions) + "."
            )
        else:
            plan["messages"].append(
                "Все рычаги уже разрешены, но физический минимум при ваших пределах "
                f"≈ {adj_eta['total_s']/3600:.1f} ч. Для меньшего времени поднимите пределы "
                "(ток/скорость стола) или огрубите геометрию (выше слой / шире шаг)."
            )
    plan["applied_settings"] = {
        "layer_height": out.layer_height,
        "hatch_spacing": out.hatch_spacing,
        "feed_bottom": out.feed_bottom_mm_min,
        "feed_top": out.feed_top_mm_min,
        "energy_bottom": out.target_energy_bottom_j_per_mm,
        "energy_top": out.target_energy_top_j_per_mm,
        "pause_bottom": out.layer_pause_bottom_s,
        "pause_top": out.layer_pause_top_s,
        "current_max": out.current_max_ma,
    }

    fcap0, fcap1 = _current_limited_feed_cap(out)
    max_feed_cap = min(fcap0, fcap1, feed_hard_max)
    wire_eta = _estimate_time_before_generation(summary, out)
    max_wire = max(wire_eta["wire_bottom"], wire_eta["wire_top"])
    needed_i0 = out.target_energy_bottom_j_per_mm * out.feed_bottom_mm_min / max(60.0 * out.voltage_kv, 1e-9)
    needed_i1 = out.target_energy_top_j_per_mm * out.feed_top_mm_min / max(60.0 * out.voltage_kv, 1e-9)

    if not plan["possible"]:
        if adj_eta["total_s"] > target_s:
            plan["messages"].append("Цель слишком короткая для выбранных ограничений тока/скорости/геометрии. Программа приблизилась к цели, но честно показывает, что полностью уложиться не получается.")
        else:
            plan["messages"].append("Цель сильно длиннее расчётного режима. Технически можно тянуть время паузами или очень малой скоростью, но это уже отдельный тепловой режим, а не простая подстройка.")

    if full_process:
        plan["messages"].append("Режим полной подстройки экспериментальный: программа может менять F, Z-шаг, шаг дорожек, паузы и Дж/мм. Итог обязательно проверять коротким TEST-файлом.")
    if out.layer_height > layer_max_quality or out.hatch_spacing > hatch_max_quality:
        plan["messages"].append("Для попадания во время увеличены Z-шаг/шаг дорожек: качество поверхности и точность формы могут стать хуже.")
    if out.layer_height < base.layer_height * 0.70 or out.hatch_spacing < base.hatch_spacing * 0.70:
        plan["messages"].append("Для растягивания времени уменьшены Z-шаг/шаг дорожек: деталь будет дольше, тепловая история изменится, но форма обычно получается аккуратнее.")
    if min(out.target_energy_bottom_j_per_mm / energy_bottom_start, out.target_energy_top_j_per_mm / energy_top_start) < 0.90:
        plan["messages"].append("Для ускорения снижена энергия Дж/мм: риск холодной ванны, тыкания проволоки и непровара выше.")
    if max(out.layer_pause_bottom_s, out.layer_pause_top_s) > max(base.layer_pause_bottom_s, base.layer_pause_top_s) + 0.5:
        plan["messages"].append("Для увеличения времени добавлены паузы между слоями: это может помочь охлаждению, но увеличит тепловую цикличность.")
    if max(out.feed_bottom_mm_min, out.feed_top_mm_min) > 1200:
        plan["messages"].append("Скорость F высокая: возрастает риск холодной ванны, тыкания проволоки и недоформирования острых углов.")
    if max_wire > out.wire_max_mm_s:
        plan["messages"].append(f"Расчётная подача проволоки до {max_wire:.2f} мм/с выше вашей контрольной границы {out.wire_max_mm_s:.2f} мм/с. Это не обрезается автоматически.")
    if max(needed_i0, needed_i1) > out.current_max_ma * 1.001:
        plan["messages"].append(f"Для выбранных Дж/мм и F нужен ток до {max(needed_i0, needed_i1):.1f} мА, но лимит пучка задан {out.current_max_ma:.1f} мА. Ток будет ограничен, энергия фактически снизится.")
    elif min(fcap0, fcap1) < max(out.feed_bottom_mm_min, out.feed_top_mm_min) * 1.02:
        plan["messages"].append(f"Скорость близка к токовому пределу: при текущих Дж/мм и U максимальная скорость около {max_feed_cap:.0f} мм/мин без клиппинга тока.")
    if target_s < base_eta["total_s"] * 0.65:
        plan["messages"].append("Запрошено заметное ускорение: ожидайте ухудшение качества по сравнению с режимом Баланс/Качество.")
    elif target_s > base_eta["total_s"] * 1.50:
        plan["messages"].append("Запрошено заметное замедление: качество может стать лучше, но тепловая история детали изменится; нужны паузы/контроль перегрева.")

    return out, plan

def _sidebar_2d_placement_controls(simple_mode: bool = False):
    with st.sidebar:
        st.header("5B. Размещение на плоскости XY")
        if simple_mode:
            rot_xy = st.selectbox("Поворот фигуры", [0, 90, 180, 270], index=0)
        else:
            rot_xy = st.number_input("Повернуть всю 2D-фигуру, град", min_value=-360.0, max_value=360.0, value=0.0, step=5.0)
        mirror_x = st.checkbox("Зеркально по X", value=False)
        mirror_y = st.checkbox("Зеркально по Y", value=False)
    return rot_xy, mirror_x, mirror_y


def _transform_polygons_2d(polys, rot_xy: float = 0.0, mirror_x: bool = False, mirror_y: bool = False):
    from shapely.affinity import rotate, scale
    out = polys
    if mirror_x or mirror_y:
        out = [scale(p, xfact=(-1.0 if mirror_x else 1.0), yfact=(-1.0 if mirror_y else 1.0), origin="center") for p in out]
    if abs(float(rot_xy or 0.0)) > 1e-12:
        out = [rotate(p, float(rot_xy), origin="center", use_radians=False) for p in out]
    return out


def draw_3d_preview_from_polys(polys, height_mm: float, title: str = "3D-модель"):
    """Simple 3D preview for 2D-based geometry by vertical extrusion."""
    if not polys:
        st.warning("Нет полигонов для 3D-предпросмотра.")
        return
    try:
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    except Exception as exc:
        st.warning(f"3D-предпросмотр недоступен: {exc}")
        return

    fig = plt.figure(figsize=(7, 5))
    ax = fig.add_subplot(111, projection="3d")
    side_faces = []
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")

    def _plot_ring(ring_coords, z, **kwargs):
        xs = [p[0] for p in ring_coords]
        ys = [p[1] for p in ring_coords]
        zs = [z for _ in ring_coords]
        ax.plot(xs, ys, zs, **kwargs)

    for poly in polys:
        rings = [list(poly.exterior.coords)] + [list(r.coords) for r in poly.interiors]
        for ring in rings:
            if len(ring) < 2:
                continue
            _plot_ring(ring, 0.0, linewidth=1.1)
            _plot_ring(ring, float(height_mm), linewidth=1.1)
            for i in range(len(ring) - 1):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[i+1][0], ring[i+1][1]
                side_faces.append([(x1, y1, 0.0), (x2, y2, 0.0), (x2, y2, float(height_mm)), (x1, y1, float(height_mm))])
                xmin = min(xmin, x1, x2)
                xmax = max(xmax, x1, x2)
                ymin = min(ymin, y1, y2)
                ymax = max(ymax, y1, y2)

    if side_faces:
        pc = Poly3DCollection(side_faces, alpha=0.22)
        ax.add_collection3d(pc)
    zmin, zmax = 0.0, float(height_mm)
    if xmin == float("inf"):
        xmin, xmax, ymin, ymax = 0.0, 1.0, 0.0, 1.0
    dx = max(xmax - xmin, 1.0)
    dy = max(ymax - ymin, 1.0)
    dz = max(zmax - zmin, 1.0)
    pad_x, pad_y, pad_z = dx * 0.08, dy * 0.08, dz * 0.08
    ax.set_xlim(xmin - pad_x, xmax + pad_x)
    ax.set_ylim(ymin - pad_y, ymax + pad_y)
    ax.set_zlim(zmin - pad_z, zmax + pad_z)
    try:
        ax.set_box_aspect((dx, dy, dz))
    except Exception:
        pass
    ax.set_title(title)
    ax.set_xlabel("X, мм")
    ax.set_ylabel("Y, мм")
    ax.set_zlabel("Z, мм")
    st.pyplot(fig)
    plt.close(fig)




def draw_rotational_vessel_3d(params: dict, title: str = "3D-модель чаши/баллона"):
    """Show a real 3D geometric preview of the rotational EBAM vessel."""
    h = max(float(params.get("height_mm", params.get("height", 80.0))), 1e-6)
    max_d = max(
        float(params.get("max_diameter_mm", 0.0)),
        float(params.get("top_diameter_mm", 0.0)),
        float(params.get("bottom_diameter_mm", 0.0)),
        1.0,
    )
    wall = max(float(params.get("wall_thickness_mm", 4.0)), 0.1)
    bottom_solid = max(float(params.get("bottom_solid_mm", max(1.0, wall))), 0.0)
    cx = cy = max_d * 0.5 + 2.0

    theta = np.linspace(0.0, 2.0 * np.pi, 120)
    zs = np.linspace(0.0, h, 120)
    Theta, Z = np.meshgrid(theta, zs)
    R_outer = np.vectorize(lambda zz: rotational_shell_outer_radius(float(zz), params))(Z)
    Xo = cx + R_outer * np.cos(Theta)
    Yo = cy + R_outer * np.sin(Theta)

    fig = plt.figure(figsize=(7.2, 5.4))
    ax = fig.add_subplot(111, projection="3d")
    ax.plot_surface(Xo, Yo, Z, alpha=0.20, linewidth=0.0, antialiased=True)

    if bottom_solid < h - 1e-6:
        inner_zs = np.linspace(min(bottom_solid, h), h, 100)
        Theta_i, Z_i = np.meshgrid(theta, inner_zs)
        R_inner_profile = np.array([max(rotational_shell_outer_radius(float(zz), params) - wall, 0.0) for zz in inner_zs])
        Xi = cx + R_inner_profile[:, None] * np.cos(Theta_i)
        Yi = cy + R_inner_profile[:, None] * np.sin(Theta_i)
        Zi = Z_i
        ax.plot_surface(Xi, Yi, Zi, alpha=0.12, linewidth=0.0, antialiased=True)

    # Bottom solid disk
    r0 = max(rotational_shell_outer_radius(0.0, params), 0.05)
    rr = np.linspace(0.0, r0, 60)
    Theta_b, R_b = np.meshgrid(theta, rr)
    Xb = cx + R_b * np.cos(Theta_b)
    Yb = cy + R_b * np.sin(Theta_b)
    Zb = np.zeros_like(Xb)
    ax.plot_surface(Xb, Yb, Zb, alpha=0.18, linewidth=0.0, antialiased=True)

    # Top rim and bottom rim
    top_r_outer = max(rotational_shell_outer_radius(h, params), 0.05)
    top_r_inner = max(top_r_outer - wall, 0.0)
    ax.plot(cx + top_r_outer * np.cos(theta), cy + top_r_outer * np.sin(theta), np.full_like(theta, h), linewidth=1.4)
    if top_r_inner > 0.05 and bottom_solid < h - 1e-6:
        ax.plot(cx + top_r_inner * np.cos(theta), cy + top_r_inner * np.sin(theta), np.full_like(theta, h), linewidth=1.0, linestyle='--')

    try:
        dx = max_d + 8.0
        dy = max_d + 8.0
        dz = max(h, 1.0)
        ax.set_box_aspect((dx, dy, dz))
    except Exception:
        pass
    ax.set_xlabel("X, мм")
    ax.set_ylabel("Y, мм")
    ax.set_zlabel("Z, мм")
    ax.set_title(title)
    ax.view_init(elev=22, azim=-55)
    st.pyplot(fig)
    plt.close(fig)


def draw_rotational_vessel_profile(params: dict):
    h = max(float(params.get("height_mm", params.get("height", 80.0))), 1e-6)
    wall = max(float(params.get("wall_thickness_mm", 4.0)), 0.1)
    bottom_solid = max(float(params.get("bottom_solid_mm", max(1.0, wall))), 0.0)
    zs = np.linspace(0.0, h, 160)
    rs = np.array([rotational_shell_outer_radius(float(z), params) for z in zs])
    inner = np.where(zs <= bottom_solid, 0.0, np.maximum(rs - wall, 0.0))

    fig, ax = plt.subplots(figsize=(6.8, 4.8))
    ax.plot(rs, zs, label="наружный профиль")
    ax.plot(-rs, zs)
    ax.plot(inner, zs, linewidth=1.0, linestyle="--", label="внутренний профиль")
    ax.plot(-inner, zs, linewidth=1.0, linestyle="--")
    ax.fill_betweenx(zs, inner, rs, alpha=0.15)
    ax.fill_betweenx(zs, -rs, -inner, alpha=0.15)
    ax.axhline(bottom_solid, linewidth=0.8, linestyle=":", label="донце/закрытая зона")
    ax.set_aspect('equal', 'box')
    ax.set_xlabel("Радиус/половина диаметра, мм")
    ax.set_ylabel("Z, мм")
    ax.set_title("Профиль чаши/баллона")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")
    st.pyplot(fig)
    plt.close(fig)


PATH_PLAN_CHOICES = [
    "Змейкой Y-", "Змейкой Y+", "Змейкой X-", "Змейкой X+",
    "Параллельно Y-", "Параллельно Y+", "Параллельно X-", "Параллельно X+",
]
SPECIAL_PATH_PLAN_CHOICES = [
    "Поворотный стол C — кольца",
    "Концентрические XY-кольца",
    "Спираль XY внутри слоя",
]
FULL_PATH_PLAN_CHOICES = [*SPECIAL_PATH_PLAN_CHOICES, *PATH_PLAN_CHOICES]
STL_PATH_PLAN_CHOICES = FULL_PATH_PLAN_CHOICES
VESSEL_PATH_PLAN_CHOICES = FULL_PATH_PLAN_CHOICES


def decode_path_plan_choice(choice: str) -> tuple[str, str, str]:
    """Return direction, deposition_strategy, thermal_ordering from one explicit hatch/parallel choice."""
    text = str(choice or "Змейкой Y-").strip()
    direction = text.split()[-1].strip().upper()
    if direction not in ("Y-", "Y+", "X-", "X+"):
        direction = "Y-"
    strategy = "continuous" if text.startswith("Змейкой") else "segmented"
    return direction, strategy, "natural"


def strategy_from_full_path_choice(choice: str, src_type: str) -> dict:
    """Map every visible operator choice to actual settings fields.

    One rule for simple and advanced modes: if a choice exists in the UI, it must
    also map to preview and G-code generation. This prevents recommendation/UI/G-code drift.
    """
    text = str(choice or "Змейкой Y-").strip()
    if text.startswith("Поворотный стол C"):
        # STL keeps a specific marker; other polygon sources use the same generic C-table generator.
        rot = "stl_rotary_c_rings" if src_type == "STL 3D" else "rotary_c_rings"
        return {"direction": "Y-", "deposition_strategy": "continuous", "thermal_ordering": "natural", "rotational_path_strategy": rot, "special": "rotary_c"}
    if text.startswith("Концентрические"):
        return {"direction": "Y-", "deposition_strategy": "continuous", "thermal_ordering": "natural", "rotational_path_strategy": "rings", "special": "rings"}
    if text.startswith("Спираль"):
        return {"direction": "Y-", "deposition_strategy": "continuous", "thermal_ordering": "natural", "rotational_path_strategy": "spiral", "special": "spiral"}
    direction, strategy, thermal = decode_path_plan_choice(text)
    return {"direction": direction, "deposition_strategy": strategy, "thermal_ordering": thermal, "rotational_path_strategy": "hatch", "special": "hatch"}


def is_special_path_choice(choice: str) -> bool:
    return str(choice or "").startswith(("Поворотный стол C", "Концентрические", "Спираль"))


def render_rotary_c_controls(key_prefix: str = "rotary_c"):
    """Editable C-table settings used by STL, чаши and generic geometry."""
    with st.expander("Настройки поворотного стола C", expanded=True):
        rotary_c_direction = st.selectbox("Направление вращения C", ["C+", "C-"], index=0, key=f"{key_prefix}_dir")
        c1, c2 = st.columns(2)
        with c1:
            rotary_c_center_x_mm = st.number_input("X центра C, мм", min_value=-5000.0, max_value=5000.0, value=0.0, step=1.0, key=f"{key_prefix}_cx")
            rotary_c_start_deg = st.number_input("Стартовый C, град", min_value=-360000.0, max_value=360000.0, value=0.0, step=10.0, key=f"{key_prefix}_start")
            rotary_c_max_deg_min = st.number_input("Максимальная скорость C, град/мин", min_value=1.0, max_value=20000.0, value=2100.0, step=50.0, key=f"{key_prefix}_max")
        with c2:
            rotary_c_center_y_mm = st.number_input("Y центра C, мм", min_value=-5000.0, max_value=5000.0, value=0.0, step=1.0, key=f"{key_prefix}_cy")
            rotary_c_b_angle_deg = st.number_input("Угол B, град", min_value=-110.0, max_value=5.0, value=0.0, step=1.0, key=f"{key_prefix}_b")
            rotary_c_min_radius_mm = st.number_input(
                "Предупреждать радиус ниже, мм (не ограничивает F при известной геометрии)",
                min_value=0.1, max_value=500.0, value=18.0, step=1.0,
                help="Порог предупреждения для малых C-радиусов. В v4.2.9.13 он не режет F, если программа знает реальные радиусы колец; C-лимит считает по реальному Rmin траектории. Для STL/неизвестной геометрии может использоваться как осторожный fallback.",
                key=f"{key_prefix}_rmin",
            )
        rotary_c_auto_limit_feed = st.checkbox("Автоограничивать F по лимиту C", value=True, key=f"{key_prefix}_autof")
        st.markdown("**Компенсация подачи по радиусу** (устраняет разгон проволоки на широких кольцах)")
        rotary_c_constant_velocity = st.checkbox(
            "Стабилизировать скорость наплавки по радиусу (constant velocity)", value=False,
            key=f"{key_prefix}_cv",
            help="Проблема (Flange1_R6): при постоянной C внешние кольца идут в 3-4 раза быстрее по ободу, и подача проволоки E2 разгоняется вслед за скоростью (до предела), а мощность не поспевает — проволока лезет из ванны. Этот режим держит линейную скорость наплавки почти постоянной: на малых радиусах C у максимума, к краю C плавно снижается (на 2-3% на кольцо), а где C упёрлась в нижний предел — шаг колец уплотняется, чтобы E2 не рос. E2 остаётся в комфортном диапазоне по всей детали.")
        rc_cv1, rc_cv2 = st.columns(2)
        with rc_cv1:
            rotary_c_wire_comfort_mm_s = st.number_input(
                "Комфортная подача E2, мм/с", min_value=1.0, max_value=60.0, value=29.0, step=0.5,
                disabled=not rotary_c_constant_velocity, key=f"{key_prefix}_e2comf",
                help="Скорость подачи проволоки, которую вы считаете стабильной (проволока уверенно в ванне). Из неё считается целевая линейная скорость наплавки по балансу объёма.")
        with rc_cv2:
            rotary_c_min_pitch_factor = st.number_input(
                "Мин. коэффициент уплотнения шага", min_value=0.3, max_value=1.0, value=0.6, step=0.05,
                disabled=not rotary_c_constant_velocity, key=f"{key_prefix}_pf",
                help="На внешних кольцах, где стол упёрся в нижний лимит C, шаг колец уплотняется, чтобы удержать E2. Это нижняя граница уплотнения (0.6 = шаг не тесней 60% исходного).")
        st.session_state[f"_cv_settings_{key_prefix}"] = {
            "rotary_c_constant_velocity": bool(rotary_c_constant_velocity),
            "rotary_c_wire_comfort_mm_s": float(rotary_c_wire_comfort_mm_s),
            "rotary_c_min_pitch_factor": float(rotary_c_min_pitch_factor),
            "rotary_c_shrink_pitch_at_floor": True,
        }
        st.session_state["_cv_settings_active"] = st.session_state[f"_cv_settings_{key_prefix}"]
        st.markdown("**Плотность аналоговых уставок E0/E2**")
        simplify_ramps = st.checkbox(
            "Упростить рампу проволоки (меньше M68, плавнее ход)", value=False, key=f"{key_prefix}_simpramp",
            help="M68 в LinuxCNC не синхронизирован с движением: на каждой смене уставки станок тормозит до нуля и рвётся сглаживание G64. По умолчанию на каждую дорожку идут три уставки (мягкий старт, основная, мягкий финиш). С этой галочкой остаётся одна уставка на дорожку — движение ровнее и быстрее, но пропадает плавный вход/выход валика на концах дорожек (возможны наплывы на краях). Радикальное решение — перевести машину на M67 после HAL-кита.")
        st.session_state["_ramp_settings_active"] = {"simplify_wire_ramps": bool(simplify_ramps)}
        st.markdown("**Тепловая выдержка между слоями (адаптивно)**")
        thermal_dwell_on = st.checkbox(
            "Минимальный цикл слоя (тепловая выдержка)", value=False, key=f"{key_prefix}_thdw",
            help="Если слой закончился быстрее минимального цикла, в конец слоя добавляется пауза G4 (луч и проволока в этот момент выключены). Длинные слои диска идут без пауз; короткие слои ступицы получают выдержку на остывание — как тепловые выдержки в R6 (2-3 мин). Количество и суммарное время печатаются в аудите.")
        tdc1, tdc2 = st.columns(2)
        with tdc1:
            thermal_cycle_min = st.number_input("Мин. цикл слоя, мин", min_value=0.5, max_value=30.0, value=3.0, step=0.5, disabled=not thermal_dwell_on, key=f"{key_prefix}_thcyc")
        with tdc2:
            thermal_floor_s = st.number_input("Мин. выдержка при срабатывании, с", min_value=0.0, max_value=600.0, value=120.0, step=10.0, disabled=not thermal_dwell_on, key=f"{key_prefix}_thflr", help="Нижний предел паузы, когда выдержка срабатывает. 120 с = «по 2 минуты на слой ступицы».")
        st.session_state["_thermal_settings_active"] = {
            "thermal_min_layer_cycle_enabled": bool(thermal_dwell_on),
            "thermal_min_layer_cycle_min": float(thermal_cycle_min),
            "thermal_min_dwell_s": float(thermal_floor_s),
        }
        rotary_c_seam_scatter_deg = st.number_input(
            "Рассеивание шва, град/кольцо (0 = выкл)", min_value=-179.0, max_value=179.0, value=0.0, step=0.5,
            help="Смещает стартовый угол каждого кольца (репозиция C при ВЫКЛЮЧЕННОМ луче), чтобы шов старт/стоп не накапливался на одной вертикали. Рекомендуется 137.5° (золотой угол). Работает в режиме относительных оборотов; в no-pause шов и так смещается углом перехода.",
            key=f"{key_prefix}_seam")
        if abs(rotary_c_b_angle_deg) > 1e-6:
            st.warning("B отличен от 0. Для первого запуска держать B=0; наклон требует TCP/центра вращения и проверки столкновений.")
    return rotary_c_center_x_mm, rotary_c_center_y_mm, rotary_c_direction, rotary_c_start_deg, rotary_c_b_angle_deg, rotary_c_max_deg_min, rotary_c_min_radius_mm, rotary_c_auto_limit_feed, rotary_c_seam_scatter_deg


def render_radial_strategy_controls(key_prefix: str = "radial"):
    rotational_radial_step_mm = st.number_input(
        "Радиальный шаг колец/спирали, мм",
        min_value=0.0, max_value=20.0, value=0.0, step=0.1,
        help="0 = использовать обычный шаг дорожек. Значение задаёт расстояние между кольцами или витками спирали.",
        key=f"{key_prefix}_step",
    )
    rotational_points_per_circle = int(st.number_input(
        "Точность окружности, сегментов/оборот",
        min_value=48, max_value=720, value=160, step=16,
        help="Чем больше сегментов, тем ровнее окружность/спираль, но тем больше файл G-code.",
        key=f"{key_prefix}_pts",
    ))
    return rotational_radial_step_mm, rotational_points_per_circle


def recommend_path_plan_for_geometry(summary: dict, src_type: str) -> dict:
    """Small deterministic heuristic so every loaded STL/geometry gets a process recommendation.

    This is not a physical simulation. It chooses a safe start mode from bounding-box shape,
    and adds an operator explanation that is recomputed on every Streamlit rerun.
    """
    sx = max(float(summary.get("size_x", 0.0) or 0.0), 1e-9)
    sy = max(float(summary.get("size_y", 0.0) or 0.0), 1e-9)
    sz = max(float(summary.get("size_z", 0.0) or 0.0), 1e-9)
    aspect_xy = sx / sy
    long_axis = "Y" if sy >= sx else "X"
    choice = f"Змейкой {long_axis}-"
    reason = f"Основной размер в плане больше по {long_axis}: так дорожки идут вдоль длинной стороны, а число соседних проходов меньше."
    risk = []
    if 0.80 <= aspect_xy <= 1.25 and sz > 0.35 * max(sx, sy):
        reason = "XY-размеры близки, а высота заметная: модель похожа на осесимметричную/объёмную. Для такой геометрии доступны C-стол, концентрические XY-кольца и XY-спираль. Для реальной Бормаш первым проверять C-стол только после сухого теста C360."
        choice = "Поворотный стол C — кольца" if src_type in ("STL 3D", "Чаши/баллоны") else "Концентрические XY-кольца"
        risk.append("Проверить, что форма действительно близка к телу вращения. Для овальной/несимметричной детали кольца/спираль/C-режим будут приближением, не точной STL-траекторией.")
    elif max(sx, sy) / max(min(sx, sy), 1e-9) > 3.0:
        risk.append("Деталь вытянутая: параллельные проходы могут быть проще для диагностики, но змейка уменьшает число включений/выключений E0/E2.")
    if src_type == "STL 3D" and not bool(summary.get("is_watertight", True)):
        risk.append("STL не замкнутый: сечения и расчёты объёма могут быть неполными, обязательно TEST.")
    return {"choice": choice, "reason": reason, "risk": risk, "aspect_xy": aspect_xy, "long_axis": long_axis}


def path_plan_caption(choice: str) -> str:
    text = str(choice or "")
    if text.startswith("Поворотный стол C"):
        return "Текущий выбор: поворотный стол C — сечения приближаются кольцами; X задаёт радиус, C делает полный оборот."
    if text.startswith("Концентрические"):
        return "Текущий выбор: концентрические XY-кольца — G-code идёт кругами через X/Y, без вращения C."
    if text.startswith("Спираль"):
        return "Текущий выбор: XY-спираль внутри слоя — меньше стартов/остановов, но нужен короткий TEST."
    direction, strategy, _ = decode_path_plan_choice(choice)
    if strategy == "continuous":
        return f"Текущий выбор: змейка/непрерывно, первый/основной проход {direction}; соседние дорожки разворачиваются автоматически."
    return f"Текущий выбор: параллельно/посегментно, все рабочие проходы {direction}."



def _rotational_strategy_ru(value: str) -> str:
    mapping = {
        "hatch": "обычное заполнение сечения выбранными дорожками",
        "rings": "концентрические XY-кольца",
        "xy_rings": "концентрические XY-кольца",
        "spiral": "XY-спираль внутри слоя",
        "xy_spiral": "XY-спираль внутри слоя",
        "rotary_c": "поворотный стол C",
        "rotary_c_rings": "поворотный стол C",
        "stl_rotary_c_rings": "поворотный стол C по STL",
        "generic_rotary_c_rings": "поворотный стол C",
        "c_rings": "поворотный стол C",
        "c_table": "поворотный стол C",
    }
    return mapping.get(str(value).lower(), str(value))



def geometry_thermal_note(summary: dict, src_type: str, settings: ProcessSettings) -> str:
    sx = float(summary.get("size_x", 0.0) or 0.0)
    sy = float(summary.get("size_y", 0.0) or 0.0)
    sz = float(summary.get("size_z", 0.0) or 0.0)
    max_xy = max(sx, sy, 1e-9)
    aspect = sx / max(sy, 1e-9)
    notes = []
    if src_type == "STL 3D":
        notes.append("STL пересчитывается после ориентации/поворота/зеркала: меняются габариты, рекомендация проходов и стартовый тепловой профиль.")
    if 0.80 <= aspect <= 1.25 and sz > 0.35 * max_xy:
        notes.append("Геометрия похожа на чашу/баллон: верх обычно перегревается сильнее, поэтому рекомендованы меньшая энергия сверху и пауза к верхним слоям.")
    elif max(sx, sy) / max(min(sx, sy), 1e-9) > 3.0:
        notes.append("Вытянутая деталь: тепловой профиль оставлен более ровным, важнее направление длинных проходов и контроль края.")
    if max_xy > 120.0:
        notes.append("Крупная деталь: время и тепловая инерция растут, таблица расчётов пересчитывает ток/проволоку/время при каждом изменении параметров.")
    if not notes:
        notes.append("Тепловой профиль рассчитан по текущим габаритам, материалу и цели. При любом изменении параметров Streamlit пересчитывает таблицу и будущий G-code.")
    notes.append(f"Сейчас рекомендовано: энергия низ/верх {settings.target_energy_bottom_j_per_mm:.1f}/{settings.target_energy_top_j_per_mm:.1f} Дж/мм; скорость низ/верх {settings.feed_bottom_mm_min:.0f}/{settings.feed_top_mm_min:.0f} мм/мин; пауза низ/верх {settings.layer_pause_bottom_s:.2f}/{settings.layer_pause_top_s:.2f} с.")
    return " ".join(notes)

def apply_material_to_settings(settings: ProcessSettings, key: str) -> ProcessSettings:
    mat = MATERIAL_LIBRARY.get(key, MATERIAL_LIBRARY["stainless_steel_12_wire"])
    settings.density_g_cm3 = float(mat.get("density_g_cm3", settings.density_g_cm3))
    settings.wire_diameter_mm = float(mat.get("wire_diameter_mm", settings.wire_diameter_mm))
    settings.target_energy_bottom_j_per_mm = float(mat.get("energy_bottom_j_mm", settings.target_energy_bottom_j_per_mm))
    settings.target_energy_top_j_per_mm = float(mat.get("energy_top_j_mm", settings.target_energy_top_j_per_mm))
    return settings


def show_parameter_effects():
    rows = [
        ["Ток / энергия", "Ванна горячее, лучше плавление", "Расплывание, шапка, боковые наплывы", "Проволока может тыкаться, непровар"],
        ["Скорость F", "Меньше Дж/мм, быстрее", "При слишком большой — холодная ванна", "При слишком малой — перегрев"],
        ["Z-шаг", "Быстрее рост, меньше слоёв", "Грубая поверхность, крупная ванна", "Дольше, но аккуратнее"],
        ["Шаг дорожек", "Меньше дорожек, быстрее", "Борозды между дорожками", "Лишнее перекрытие, перегрев"],
        ["Подача проволоки", "Больше металла", "Шапка/распухание", "Просадка, тонкий валик"],
        ["Контурные проходы", "Лучше форма края", "Сложнее для боковой проволоки", "Без них край грубее"],
        ["W-ретракт", "Меньше хвостов", "При конфликте с M68 — опасно", "Без него больше хвосты"],
    ]
    st.dataframe(pd.DataFrame(rows, columns=["Параметр", "Если увеличить", "Основной риск", "Если уменьшить"]), hide_index=True, width="stretch")


def build_settings(summary, mode, material_key, advanced_mode: bool = True):
    recommended = recommend_settings_from_summary(summary, mode, material_key=material_key)
    path_reco = recommend_path_plan_for_geometry(summary, src_type)
    thermal_note_text = geometry_thermal_note(summary, src_type, recommended)
    path_reco_choice = path_reco.get("choice", "Змейкой Y-")
    path_reco_index = PATH_PLAN_CHOICES.index(path_reco_choice) if path_reco_choice in PATH_PLAN_CHOICES else 0

    if not advanced_mode:
        with st.sidebar:
            st.header("6. Автоматический расчёт")
            st.caption("В простом режиме меняются только основные ограничения. Остальное приложение берёт из базы по материалу, геометрии и цели.")
            st.info("Тепловой профиль: " + thermal_note_text)
            voltage = st.number_input("Ускоряющее напряжение U, кВ", min_value=1.0, max_value=120.0, value=float(recommended.voltage_kv), step=1.0)
            st.session_state.setdefault("w_s_layer", float(recommended.layer_height))
            layer_height = st.number_input(
                "Шаг слоя Z, мм",
                min_value=0.05, max_value=10.0, step=0.01, key="w_s_layer",
                help="Высота одного слоя по Z. Меньше — точнее и дольше; больше — быстрее, но выше риск грубой поверхности и недоплава."
            )
            if layer_height > 2.0:
                st.warning("Z-шаг выше 2 мм — экспериментальный режим. Проверять только коротким TEST.")
            st.session_state.setdefault("w_s_hatch", float(recommended.hatch_spacing))
            hatch_spacing = st.number_input(
                "Шаг/ширина между дорожками, мм",
                min_value=0.2, max_value=10.0, step=0.05, key="w_s_hatch",
                help="Расстояние между соседними дорожками или радиальный шаг для колец/спирали, если отдельный радиальный шаг = 0."
            )
            # v4.2.9.31: travel speed is now editable in simple mode too (previously
            # it was buried in the recommender and only reachable in advanced mode).
            st.session_state.setdefault("w_s_f0", float(recommended.feed_bottom_mm_min))
            st.session_state.setdefault("w_s_f1", float(recommended.feed_top_mm_min))
            _sf1, _sf2 = st.columns(2)
            with _sf1:
                simple_f0 = st.number_input(
                    "Скорость низ F, мм/мин", min_value=10.0, max_value=3000.0, step=10.0,
                    format="%.1f", key="w_s_f0",
                    help="Скорость движения при наплавке нижних слоёв. Выше скорость — быстрее деталь, но на каждый миллиметр пути ложится меньше металла и меньше тепла.")
            with _sf2:
                simple_f1 = st.number_input(
                    "Скорость верх F, мм/мин", min_value=10.0, max_value=3000.0, step=10.0,
                    format="%.1f", key="w_s_f1",
                    help="Скорость на верхних слоях. Обычно чуть выше нижней: верх детали остывает хуже.")
            # Live volumetric energy density readout — the number that actually decides
            # whether the bead fuses (too low) or spatters (too high).
            _sec = float(layer_height) * float(hatch_spacing)
            _qv_now = float(recommended.target_energy_bottom_j_per_mm) / max(_sec, 1e-9)
            _p_now = float(recommended.target_energy_bottom_j_per_mm) * float(simple_f0) / 60.0
            if _qv_now >= 110.0:
                st.error(f"↳ Плотность энергии QV ≈ {_qv_now:.0f} Дж/мм³ — ПЕРЕГРЕВ (норма 55–90). Увеличьте слой/шаг или снизьте энергию.")
            elif _qv_now <= 42.0:
                st.error(f"↳ Плотность энергии QV ≈ {_qv_now:.0f} Дж/мм³ — НЕ ПРОПЛАВИТ (норма 55–90). Уменьшите слой/шаг или поднимите энергию.")
            elif _qv_now > 90.0 or _qv_now < 55.0:
                st.warning(f"↳ Плотность энергии QV ≈ {_qv_now:.0f} Дж/мм³ — вне рабочей полосы 55–90.")
            else:
                st.caption(f"↳ Плотность энергии QV ≈ {_qv_now:.0f} Дж/мм³ (норма 55–90) · мощность ≈ {_p_now:.0f} Вт · сечение валика {_sec:.3f} мм²")
            st.session_state.setdefault("w_s_imax", float(recommended.current_max_ma))
            current_max = st.number_input(
                "Лимит тока пучка E0, мА",
                min_value=1.0, max_value=1000.0, step=5.0, key="w_s_imax",
                help="Программа может использовать ток только до этого лимита. Самостоятельно лимит не повышается."
            )
            if current_max > 100.0:
                st.warning("Лимит выше 100 мА — тяжёлая экспериментальная зона. Проверять только через TEST и штатные interlocks.")
            beam_current_mode = "energy"
            beam_current_bottom_ma = float(recommended.target_energy_bottom_j_per_mm * float(st.session_state.get("w_s_f0", recommended.feed_bottom_mm_min)) / max(60.0 * recommended.voltage_kv, 1e-9))
            beam_current_top_ma = float(recommended.target_energy_top_j_per_mm * float(st.session_state.get("w_s_f1", recommended.feed_top_mm_min)) / max(60.0 * recommended.voltage_kv, 1e-9))
            with st.expander("Уставка тока E0, если нужно вручную", expanded=False):
                use_current_setpoint = st.checkbox(
                    "Задавать ток E0 напрямую",
                    value=False,
                    help="Если включено, E0 берётся из уставки тока, а фактическая энергия Дж/мм пересчитывается от тока и скорости. Это полезно для экспериментов, где оператор хочет управлять именно током пучка."
                )
                if use_current_setpoint:
                    beam_current_mode = "current"
                    col_cb, col_ct = st.columns(2)
                    with col_cb:
                        beam_current_bottom_ma = st.number_input("Уставка E0 низ, мА", min_value=0.0, max_value=float(current_max), value=min(beam_current_bottom_ma, float(current_max)), step=0.5, format="%.3f")
                    with col_ct:
                        beam_current_top_ma = st.number_input("Уставка E0 верх, мА", min_value=0.0, max_value=float(current_max), value=min(beam_current_top_ma, float(current_max)), step=0.5, format="%.3f")
                    st.caption("При изменении скорости F энергия Дж/мм будет изменяться автоматически: E = U·I / V.")
                else:
                    st.caption("Обычный режим: задаётся энергия Дж/мм, а ток E0 рассчитывается автоматически.")
            wire_max = st.number_input(
                "Контрольная подача проволоки, мм/с",
                min_value=0.1, max_value=200.0, value=float(recommended.wire_max_mm_s), step=0.5,
                help="Это граница предупреждения, а не автоматический зажим. Если привод реально может больше — поднимите границу."
            )

            rotational_plan_mode = (src_type == "Чаши/баллоны")
            rotational_path_strategy = "hatch"
            rotational_radial_step_mm = 0.0
            rotational_points_per_circle = 160
            rotary_c_center_x_mm = 0.0
            rotary_c_center_y_mm = 0.0
            rotary_c_direction = "C+"
            rotary_c_start_deg = 0.0
            rotary_c_b_angle_deg = 0.0
            rotary_c_max_deg_min = 2100.0
            rotary_c_min_radius_mm = 18.0
            rotary_c_seam_scatter_deg = 0.0
            rotary_c_auto_limit_feed = True
            st.header("7. Полный набор стратегий проходов")
            path_choices = FULL_PATH_PLAN_CHOICES
            path_reco_index = path_choices.index(path_reco_choice) if path_reco_choice in path_choices else 0
            if src_type == "STL 3D":
                st.info("Рекомендация по STL: " + path_reco_choice + ". " + path_reco.get("reason", ""))
                for msg in path_reco.get("risk", []):
                    st.warning(msg)
            elif rotational_plan_mode:
                st.caption("Для чаш/баллонов доступны все стратегии: C-стол, XY-кольца, XY-спираль, змейка и параллельные проходы. Выбор ниже напрямую идёт в G-code.")
            else:
                st.caption("Полный список стратегий доступен для любого источника. Для неосесимметричных деталей C/кольца/спираль являются приближением — проверяйте preview и TEST.")
            simple_plan = st.radio(
                "Как вести траекторию",
                path_choices,
                index=path_reco_index,
                help="Единый список для простого режима: C-стол, концентрические XY-кольца, XY-спираль, змейка и параллельные проходы. Если пункт есть в меню, он реально применяется в preview и G-code."
            )
            decoded_plan = strategy_from_full_path_choice(simple_plan, src_type)
            simple_direction = decoded_plan["direction"]
            simple_strategy = decoded_plan["deposition_strategy"]
            rotational_path_strategy = decoded_plan["rotational_path_strategy"]
            if decoded_plan["special"] == "rotary_c":
                st.success("Будут построены C-кольца: B фиксирован, X задаёт радиус, Z слой, C делает G91 C360. Проверить C на сухом прогоне.")
                (rotary_c_center_x_mm, rotary_c_center_y_mm, rotary_c_direction, rotary_c_start_deg,
                 rotary_c_b_angle_deg, rotary_c_max_deg_min, rotary_c_min_radius_mm,
                 rotary_c_auto_limit_feed, rotary_c_seam_scatter_deg) = render_rotary_c_controls("simple_c")
            elif decoded_plan["special"] == "rings":
                st.success("Будут построены концентрические XY-кольца. Для круглых деталей это технологичнее обычной змейки; для некруглых — приближение.")
            elif decoded_plan["special"] == "spiral":
                st.info("Будет построена XY-спираль внутри слоя. Меньше стартов/остановов, но нужен короткий TEST.")
            if decoded_plan["special"] in ("rotary_c", "rings", "spiral"):
                rotational_radial_step_mm, rotational_points_per_circle = render_radial_strategy_controls("simple_radial")
            st.caption(path_plan_caption(simple_plan))

            st.header("8. Целевое время")
            st.session_state.setdefault("w_tt_simple", False)
            target_time_enabled = st.checkbox("Подогнать режим под желаемое время", key="w_tt_simple")
            col_th, col_tm = st.columns(2)
            with col_th:
                target_hours = st.number_input("Часы", min_value=0, max_value=200, value=0, step=1, disabled=not target_time_enabled)
            with col_tm:
                target_minutes = st.number_input("Минуты", min_value=0, max_value=59, value=30, step=5, disabled=not target_time_enabled)
            st.caption("В простом режиме для подгонки времени программа может менять все нужные расчётные параметры, но не повышает лимит тока сама.")

            with st.expander("Дополнительно, обычно не трогать"):
                center_xy = st.checkbox("Центрировать XY вокруг нуля", value=False)
                add_contour = st.checkbox("Добавить 1 контурный проход для края", value=(mode != "Скорость"))
                include_comments = st.checkbox("Комментарии в G-code", value=False, help="Для больших STL лучше выключить: файл будет меньше.")

        settings = replace(
            recommended,
            voltage_kv=voltage,
            layer_height=float(layer_height),
            hatch_spacing=float(hatch_spacing),
            feed_bottom_mm_min=float(simple_f0),
            feed_top_mm_min=float(simple_f1),
            current_min_ma=0.0,
            current_low_warning_ma=1.0,
            current_max_ma=current_max,
            beam_current_mode=beam_current_mode,
            beam_current_bottom_ma=float(beam_current_bottom_ma),
            beam_current_top_ma=float(beam_current_top_ma),
            wire_max_mm_s=wire_max,
            direction=simple_direction,
            deposition_strategy=simple_strategy,
            rotational_path_strategy=rotational_path_strategy,
            rotational_radial_step_mm=float(rotational_radial_step_mm),
            rotational_points_per_circle=int(rotational_points_per_circle),
            rotary_c_center_x_mm=float(rotary_c_center_x_mm),
            rotary_c_center_y_mm=float(rotary_c_center_y_mm),
            rotary_c_direction=str(rotary_c_direction),
            rotary_c_start_deg=float(rotary_c_start_deg),
            rotary_c_b_angle_deg=float(rotary_c_b_angle_deg),
            rotary_c_max_deg_min=float(rotary_c_max_deg_min),
            rotary_c_min_radius_mm=float(rotary_c_min_radius_mm),
            rotary_c_auto_limit_feed=bool(rotary_c_auto_limit_feed),
            rotary_c_seam_scatter_deg=float(rotary_c_seam_scatter_deg),
            thermal_ordering="natural",
            alternate_layer_rotation=False,
            center_xy=center_xy,
            contour_passes=(1 if add_contour else 0),
            contour_every_n_layers=1,
            include_comments=include_comments,
            adaptive_thin_wall=True,
            force_contour_on_empty_layers=True,
            adaptive_section_probe=True,
            adaptive_wire_correction=True,
            manual_section_fallback=True,
            projection_fallback_if_empty=False,
        )
        target_total_s = (int(target_hours) * 3600 + int(target_minutes) * 60) if target_time_enabled else 0
        time_plan = {"enabled": False, "messages": [], "target_s": 0.0, "base_total_s": 0.0, "adjusted_total_s": 0.0, "possible": True, "severity": "ok"}
        if target_time_enabled and target_total_s > 0:
            settings, time_plan = _fit_settings_to_target_time(summary, settings, float(target_total_s), "full_process")
        _cv_apply = st.session_state.get("_cv_settings_active") or {}
        if _cv_apply.get("rotary_c_constant_velocity"):
            settings = replace(settings, **_cv_apply)
        _th_apply = st.session_state.get("_thermal_settings_active") or {}
        if _th_apply.get("thermal_min_layer_cycle_enabled"):
            settings = replace(settings, **_th_apply)
        _rp_apply = st.session_state.get("_ramp_settings_active") or {}
        if _rp_apply.get("simplify_wire_ramps"):
            settings = replace(settings, **_rp_apply)
        settings, c_limit_info = _apply_rotary_c_feed_limit_to_settings(summary, settings)
        if c_limit_info:
            time_plan["c_limit_info"] = c_limit_info
            with st.sidebar:
                if c_limit_info.get("limited"):
                    st.warning(c_limit_info["operator_message"])
                else:
                    st.info(c_limit_info["operator_message"])
        return settings, time_plan

    with st.sidebar:
        st.header("6. Технология")
        st.caption("Значения ниже уже рекомендованы по геометрии и материалу, но их можно править.")
        voltage = st.number_input("Ускоряющее напряжение, кВ", min_value=1.0, max_value=120.0, value=float(recommended.voltage_kv), step=1.0)
        st.session_state.setdefault("w_adv_layer", float(recommended.layer_height))
        layer_height = st.number_input("Шаг слоя Z, мм", min_value=0.05, max_value=10.0, step=0.01, key="w_adv_layer", help="2–10 мм — экспериментальный крупный слой, только для коротких TEST.")
        if layer_height > 2.0:
            st.warning("Z-шаг выше 2 мм — экспериментальный режим. Проверять только через короткий TEST: резко растёт подача проволоки и риск недоплава/тыкания проволоки.")
        st.session_state.setdefault("w_adv_hatch", float(recommended.hatch_spacing))
        hatch_spacing = st.number_input("Шаг дорожек, мм", min_value=0.2, max_value=10.0, step=0.05, key="w_adv_hatch")
        if float(hatch_spacing) > 0:
            _bw_hint = float(hatch_spacing) / 0.738
            st.caption(f"↳ при валике ~{_bw_hint:.1f} мм это TOM-перекрытие 26%; валиков на 100 мм ширины: {100.0/float(hatch_spacing):.0f}")
        wire_d = st.number_input("Диаметр проволоки, мм", min_value=0.2, max_value=5.0, value=float(recommended.wire_diameter_mm), step=0.1)
        deposition_efficiency = st.number_input(
            "Коэффициент осаждения η", min_value=0.10, max_value=1.00,
            value=float(getattr(recommended, "deposition_efficiency", 1.0)), step=0.01, format="%.3f",
            help="Доля поданной проволоки, формирующая полезный валик. 1.0 сохраняет старую модель без потерь, но считается некалиброванной верхней оценкой."
        )
        if deposition_efficiency >= 0.999:
            st.warning("η=1.000 — расчёт без потерь металла. Для абсолютной E2/толщины лучше определить η по короткому калибровочному образцу.")
        wire_min = st.number_input("Нижняя контрольная подача проволоки, мм/с", min_value=0.0, max_value=200.0, value=float(recommended.wire_min_mm_s), step=0.1, help="Контрольная граница для предупреждения, не автоматический зажим G-code.")
        wire_max = st.number_input("Верхняя контрольная подача проволоки, мм/с", min_value=0.1, max_value=200.0, value=float(recommended.wire_max_mm_s), step=0.5, help="Если расчётная подача выше этой границы, приложение предупредит. Саму подачу оно не обрежет.")
        density = st.number_input("Плотность, г/см³", min_value=0.5, max_value=25.0, value=float(recommended.density_g_cm3), step=0.1)
        center_xy = st.checkbox("Центрировать XY вокруг нуля", value=False, help="Для Bormash обычно выключить, чтобы X/Y были положительными.")

        st.header("7. Тепловой профиль")
        st.info("Тепловой профиль: " + thermal_note_text)
        use_current_setpoint = st.checkbox(
            "Задавать ток E0 напрямую вместо энергии Дж/мм",
            value=False,
            help="Обычный режим: задаёте Дж/мм, программа считает ток. Режим уставки: задаёте E0, программа считает фактические Дж/мм от тока и скорости. Поля энергии в этом режиме заблокированы намеренно."
        )
        st.session_state.setdefault("w_adv_e0", float(recommended.target_energy_bottom_j_per_mm))
        e0 = st.number_input(
            "Энергия низ, Дж/мм", min_value=10.0, max_value=1000.0, step=1.0, key="w_adv_e0",
            disabled=bool(use_current_setpoint),
            help="Активно только в режиме расчёта по энергии. Если включена уставка E0, фактическая энергия считается от тока и скорости."
        )
        st.session_state.setdefault("w_adv_e1", float(recommended.target_energy_top_j_per_mm))
        e1 = st.number_input(
            "Энергия верх, Дж/мм", min_value=10.0, max_value=1000.0, step=1.0, key="w_adv_e1",
            disabled=bool(use_current_setpoint),
            help="Активно только в режиме расчёта по энергии. Если включена уставка E0, фактическая энергия считается от тока и скорости."
        )
        if use_current_setpoint:
            st.caption("Поля энергии затемнены: сейчас первична уставка E0, а Дж/мм пересчитываются автоматически. Скорость F и подача проволоки остаются доступными.")
        st.session_state.setdefault("w_adv_f0", float(recommended.feed_bottom_mm_min))
        st.session_state.setdefault("w_adv_f1", float(recommended.feed_top_mm_min))
        f0 = st.number_input("Скорость низ F, мм/мин", min_value=10.0, max_value=3000.0, step=10.0, format="%.1f", key="w_adv_f0")
        f1 = st.number_input("Скорость верх F, мм/мин", min_value=10.0, max_value=3000.0, step=10.0, format="%.1f", key="w_adv_f1")
        st.caption("Скорость движения задаётся в мм/мин. Это же значение записывается в G-code как F для LinuxCNC/G-code.")
        beam_current_mode = "energy"
        beam_current_bottom_ma = float(recommended.target_energy_bottom_j_per_mm * f0 / max(60.0 * voltage, 1e-9))
        beam_current_top_ma = float(recommended.target_energy_top_j_per_mm * f1 / max(60.0 * voltage, 1e-9))
        st.session_state.setdefault("w_adv_imax", float(recommended.current_max_ma))
        current_max = st.number_input(
            "Верхний лимит тока пучка E0, мА",
            min_value=1.0, max_value=1000.0, step=5.0, key="w_adv_imax",
            help="Это программный/технологический лимит для расчёта и проверки. Приложение не поднимает его само при подгонке времени. Значения выше 100 мА для EBAM-пуска считать опасной экспериментальной зоной."
        )
        current_min = st.number_input(
            "Нижний лимит тока пучка E0, мА",
            min_value=0.0, max_value=1000.0, value=float(recommended.current_min_ma), step=0.5, format="%.3f",
            help="Жёсткий нижний зажим тока в G-code. 0 мА = не поднимать ток искусственно, а записывать расчётное значение."
        )
        current_low_warning = st.number_input(
            "Предупреждать о малом токе ниже, мА",
            min_value=0.0, max_value=1000.0, value=float(recommended.current_low_warning_ma), step=0.5, format="%.3f",
            help="Только предупреждение для оператора. Этот порог не меняет G-code и не поднимает ток."
        )
        min_beam_power_w = st.number_input(
            "Порог мощности проплава, Вт (0 = выкл)",
            min_value=0.0, max_value=50000.0, value=float(getattr(recommended, "min_beam_power_w", 900.0)), step=50.0, format="%.0f",
            help="Совещательный порог по реальному опыту: Дж/мм НЕ гарантируют проплав — при низкой скорости даже 'правильная' энергия даёт малую мощность P=U·I и непроплав/шарики (реальный случай: 160 Дж/мм + медленное C ≈ 630 Вт). G-code не меняется, ток не поднимается. Калибруется одиночным валиком."
        )
        power_floor_warning_enabled = st.checkbox(
            "Включить проверку порога мощности", value=bool(getattr(recommended, "power_floor_warning_enabled", True)),
            help="Если выключить — предупреждение о малой мощности проплава не показывается."
        )
        if current_min > current_max:
            st.error("Нижний лимит тока выше верхнего. Уменьшите нижний лимит или увеличьте верхний.")
            current_min = current_max
        if current_max > 100.0:
            st.warning("Лимит тока выше 100 мА: это уже тяжёлый тепловой режим. Для реальной установки проверяйте паспорт источника, охлаждение, вакуум, защиту, interlocks и начинайте только с короткого TEST-файла.")
        if use_current_setpoint:
            beam_current_mode = "current"
            col_cb, col_ct = st.columns(2)
            with col_cb:
                beam_current_bottom_ma = st.number_input("Уставка тока E0 низ, мА", min_value=0.0, max_value=float(current_max), value=min(float(beam_current_bottom_ma), float(current_max)), step=0.5, format="%.3f", help="Ток E0 для нижних слоёв. Фактическая энергия Дж/мм будет рассчитана от этого тока и скорости F.")
            with col_ct:
                beam_current_top_ma = st.number_input("Уставка тока E0 верх, мА", min_value=0.0, max_value=float(current_max), value=min(float(beam_current_top_ma), float(current_max)), step=0.5, format="%.3f", help="Ток E0 для верхних слоёв. Между низом и верхом ток интерполируется по высоте.")
            st.info("Включена уставка тока: изменение F теперь меняет фактические Дж/мм. Это удобно для экспериментов, но требует контроля ванны и TEST.")
        focus = st.number_input("Фокус E1, мА", min_value=0.0, max_value=3000.0, value=float(recommended.focus_ma), step=1.0)

        rotational_plan_mode = (src_type == "Чаши/баллоны")
        rotational_path_strategy = "hatch"
        rotational_radial_step_mm = 0.0
        rotational_points_per_circle = 160
        rotary_c_center_x_mm = 0.0
        rotary_c_center_y_mm = 0.0
        rotary_c_direction = "C+"
        rotary_c_start_deg = 0.0
        rotary_c_b_angle_deg = 0.0
        rotary_c_max_deg_min = 2100.0
        rotary_c_min_radius_mm = 18.0
        rotary_c_seam_scatter_deg = 0.0
        rotary_c_auto_limit_feed = True
        rotary_c_motion_mode = "separate_rings"
        rotary_c_transition_angle_deg = 17.0
        rotary_c_continuous_keep_beam_wire_on = False
        rotary_c_disable_layer_pauses = False
        rotary_c_disable_w_retract = False
        rotary_c_disable_z_hop = False
        wire_feed_mode = "auto"
        wire_feed_manual_mm_s = 0.0
        wire_feed_bottom_mm_s = 0.0
        wire_feed_top_mm_s = 0.0
        path_control_mode = "g64_tolerance"
        g64_naive_cam_q_mm = 0.0
        analog_output_mode = "m68_compatible"
        machine_m67_confirmed = False
        rotary_c_radius_variation_tolerance_mm = 0.05
        st.header("8. Полный набор стратегий проходов")
        path_choices = FULL_PATH_PLAN_CHOICES
        path_reco_index_full = path_choices.index(path_reco_choice) if path_reco_choice in path_choices else 0
        if src_type == "STL 3D":
            st.caption("Для STL доступны все стратегии: точные X/Y-дорожки, XY-кольца/спираль и поворотный стол C. Для C/колец/спирали STL приближается через радиусы сечения, поэтому годится прежде всего для тел вращения.")
            st.info("Рекомендация по STL: " + path_reco_choice + ". " + path_reco.get("reason", ""))
            for msg in path_reco.get("risk", []):
                st.warning(msg)
        elif rotational_plan_mode:
            st.caption("Для чаш/баллонов доступны все стратегии. Рекомендуемые для шар-баллона: поворотный стол C или концентрические XY-кольца. Змейка/параллельные оставлены для сравнения и диагностики.")
        else:
            st.caption("Единый список для всех источников. Если геометрия не круглая, специальные кольцевые/спиральные/C-режимы будут приближением, а обычные X/Y дорожки точнее повторят форму.")
        path_choice = st.radio(
            "Стратегия траектории",
            path_choices,
            index=path_reco_index_full,
            help="Полный набор в расширенном режиме: C-стол, концентрические XY-кольца, XY-спираль, змейка и параллельные проходы. Выбор напрямую задаёт settings и G-code.",
        )
        decoded_plan = strategy_from_full_path_choice(path_choice, src_type)
        direction = decoded_plan["direction"]
        deposition_strategy = decoded_plan["deposition_strategy"]
        thermal_ordering = decoded_plan["thermal_ordering"]
        rotational_path_strategy = decoded_plan["rotational_path_strategy"]
        if decoded_plan["special"] == "rotary_c":
            st.success("Будет сгенерирован режим поворотного стола C: сечения переводятся в радиусы, X=центр C+R, C делает G91 C360.")
            (rotary_c_center_x_mm, rotary_c_center_y_mm, rotary_c_direction, rotary_c_start_deg,
             rotary_c_b_angle_deg, rotary_c_max_deg_min, rotary_c_min_radius_mm,
             rotary_c_auto_limit_feed, rotary_c_seam_scatter_deg) = render_rotary_c_controls("adv_c")
            with st.expander("C-стол: непрерывность, одно кольцо и отключение лишних движений", expanded=True):
                motion_ru = st.selectbox(
                    "Режим C-колец",
                    ["Обычные отдельные кольца", "Одно кольцо, C без остановки, переход C+Z"],
                    index=0,
                    help="Второй режим: C360 выполняется на фиксированном Z, затем C продолжает небольшой угол и одновременно поднимает Z на шаг слоя. E0/E2 не выключаются между кольцами."
                )
                if motion_ru.startswith("Одно"):
                    rotary_c_motion_mode = "no_pause_flat_rings"
                    rotary_c_continuous_keep_beam_wire_on = True  # обязательное условие режима, галочка не нужна
                    st.info("Обязательное в этом режиме: луч E0 и проволока E2 включаются один раз в начале и выключаются только в конце. Это не опция, поэтому отдельной галочки нет.")
                    col_np1, col_np2 = st.columns(2)
                    with col_np1:
                        rotary_c_transition_angle_deg = st.number_input("Угол перехода C+Z между слоями, град", min_value=0.0, max_value=180.0, value=17.0, step=1.0, help="После каждого C360 стол продолжает этот угол и за него Z поднимается на шаг слоя. 0° может снова дать остановку C.")
                        rotary_c_disable_layer_pauses = st.checkbox("Отключить паузы/прогрев/settle между слоями", value=True)
                    with col_np2:
                        rotary_c_disable_w_retract = st.checkbox("Отключить W-ретракт в C-no-pause", value=True)
                        rotary_c_disable_z_hop = st.checkbox("Отключить Z-hop в C-no-pause", value=True)
                    st.warning("Перед запуском: dry run без луча и проволоки. Проверить, что C не останавливается после 360°, а переход C+Z идёт плавно.")
                else:
                    rotary_c_motion_mode = "separate_rings"
        elif decoded_plan["special"] == "rings":
            st.success("Будут сгенерированы концентрические XY-кольца по сечению. Это хорошо для круглых/осесимметричных деталей, но является приближением для некруглых STL.")
        elif decoded_plan["special"] == "spiral":
            st.info("Будет сгенерирована XY-спираль внутри слоя. Меньше стартов/остановов; обязательно проверять коротким TEST.")
        else:
            if deposition_strategy == "segmented":
                thermal_skip = st.checkbox("Тепловое чередование через одну дорожку", value=False, help="Если включено, порядок параллельных дорожек будет через одну для снижения локального перегрева.")
                thermal_ordering = "skip_neighbours" if thermal_skip else "natural"
        if decoded_plan["special"] in ("rotary_c", "rings", "spiral"):
            rotational_radial_step_mm, rotational_points_per_circle = render_radial_strategy_controls("adv_radial")
        with st.expander("Подача проволоки E2: авторасчёт или ручная уставка", expanded=(decoded_plan["special"] == "rotary_c")):
            wire_mode_ru = st.radio(
                "Как задавать E2",
                ["Авто: пересчитать по F, Z и шагу дорожки", "Ручная постоянная E2", "Ручная E2 низ/верх"],
                index=0,
                help="Авто сохраняет расчётный объём металла при изменении скорости. Ручной режим позволяет поставить фактическую подачу проволоки и сразу увидеть пересчёт энергии/объёма/толщины."
            )
            if wire_mode_ru.startswith("Ручная постоянная"):
                wire_feed_mode = "manual_constant"
                wire_feed_manual_mm_s = st.number_input("E2 постоянная, мм/с", min_value=0.0, max_value=100.0, value=3.30, step=0.05, format="%.3f")
                st.caption("В G-code будет одна уставка E2. Геометрический расход и энергия на объём пересчитаются от этой подачи.")
            elif wire_mode_ru.startswith("Ручная E2"):
                wire_feed_mode = "manual_bottom_top"
                col_wb, col_wt = st.columns(2)
                with col_wb:
                    wire_feed_bottom_mm_s = st.number_input("E2 низ, мм/с", min_value=0.0, max_value=100.0, value=3.30, step=0.05, format="%.3f")
                with col_wt:
                    wire_feed_top_mm_s = st.number_input("E2 верх, мм/с", min_value=0.0, max_value=100.0, value=3.30, step=0.05, format="%.3f")
            else:
                wire_feed_mode = "auto"
                st.caption("E2 будет автоматически пересчитана, если ты меняешь F, Z-шаг, шаг дорожек или C-лимит.")
        st.info(path_plan_caption(path_choice))

        alternate_layer_rotation = st.checkbox("Чередовать X/Y по слоям", value=False)
        link_feed_factor = st.number_input("Скорость перехода змейки между дорожками (×F)", min_value=1.0, max_value=5.0, value=1.30, step=0.05, disabled=(deposition_strategy != "continuous"))

        with st.expander("Перекрытие валиков и авто-шаг дорожек", expanded=False):
            bead_width_mm = st.number_input("Ширина валика из TEST, мм (0 = не использовать)", min_value=0.0, max_value=20.0, value=0.0, step=0.1)
            overlap_model_ru = st.selectbox("Модель перекрытия валиков", ["TOM 0.738·w (Ding 2015)", "FOM 0.667·w (Suryakumar 2011)"], index=0)
            overlap_model = "tom" if overlap_model_ru.startswith("TOM") else "fom"
            auto_hatch_from_bead = st.checkbox("Брать шаг дорожек из ширины валика", value=False)
            if auto_hatch_from_bead and bead_width_mm > 0:
                from ebam_gcode_studio.core import recommended_hatch_from_bead as _rh
                st.info(f"Шаг дорожек будет = {_rh(bead_width_mm, overlap_model):.3f} мм.")

        with st.expander("Геометрия контура и восстановление сложных STL", expanded=False):
            edge_offset = st.number_input("Отступ от края контура, мм", min_value=0.0, max_value=10.0, value=float(recommended.edge_offset), step=0.1)
            contour_passes = st.number_input("Контурных проходов на слой", min_value=0, max_value=5, value=int(recommended.contour_passes), step=1)
            contour_every = st.number_input("Контур каждые N слоёв", min_value=1, max_value=20, value=int(recommended.contour_every_n_layers), step=1)
            contour_first = st.checkbox("Сначала контур, потом штриховка", value=False)
            adaptive = st.checkbox("Адаптивная тонкая стенка STL", value=True)
            adaptive_probe = st.checkbox("Устойчивый поиск STL-сечения по Z", value=True)
            adaptive_wire = st.checkbox("Коррекция проволоки для тонких слоёв", value=True)
            manual_fallback = st.checkbox("Офлайн-резерв построения STL-сечения", value=True)
            projection_fallback = st.checkbox("Последний резерв: XY-проекция STL", value=False)

        st.header("9. Старт/финиш и безопасность")
        zhop = st.number_input("Z-hop, мм", min_value=0.0, max_value=50.0, value=float(recommended.z_hop_mm), step=0.5)
        safe_initial_approach_enabled = st.checkbox(
            "Отдельный безопасный Z для начального подвода",
            value=False,
            help="Если включено, до B/C и XY позиционирования выполняется G0 на заданный абсолютный Z. Значение зависит от оснастки и рабочих смещений; обязательно проверить сухим прогоном.",
        )
        safe_initial_approach_z_mm = st.number_input(
            "Безопасный Z начального подвода, мм",
            min_value=0.0,
            max_value=1000.0,
            value=7.0,
            step=0.5,
            disabled=not safe_initial_approach_enabled,
        )
        use_w = st.checkbox("Использовать W-ретракт", value=True)
        wret = st.number_input("W-ретракт, мм", min_value=0.0, max_value=10.0, value=float(recommended.w_retract_mm), step=0.1)
        lead = st.number_input("Lead-in без проволоки, мм", min_value=0.0, max_value=20.0, value=float(recommended.lead_in_beam_mm), step=0.1)
        soft_start = st.number_input("Мягкий старт, мм", min_value=0.0, max_value=30.0, value=float(recommended.soft_start_mm), step=0.1)
        soft_finish = st.number_input("Мягкий финиш, мм", min_value=0.0, max_value=30.0, value=float(recommended.soft_finish_mm), step=0.1)
        include_comments = st.checkbox("Комментарии в G-code", value=False, help="Выключайте для больших STL: файл будет меньше и быстрее загрузится в LinuxCNC.")

        with st.expander("Контроллер, сглаживание и аналоговые E0/E2", expanded=(decoded_plan["special"] == "rotary_c")):
            path_mode_ru = st.selectbox(
                "Режим планировщика траектории",
                ["G64 P/Q — плавное сопряжение", "Режим станка по умолчанию", "G61 — точная траектория", "G61.1 — точная остановка"],
                index=0,
                help="Для непрерывного C обычно нужен G64. G61/G61.1 могут замедлять или останавливать оси на границах блоков."
            )
            if path_mode_ru.startswith("G64"):
                path_control_mode = "g64_tolerance"
            elif path_mode_ru.startswith("Режим"):
                path_control_mode = "machine_default"
            elif path_mode_ru.startswith("G61.1"):
                path_control_mode = "g61_1"
            else:
                path_control_mode = "g61"
            pc1, pc2 = st.columns(2)
            with pc1:
                g64_tolerance = st.number_input(
                    "Допуск G64 P, мм", min_value=0.0, max_value=10.0,
                    value=float(recommended.g64_tolerance_mm), step=0.01, format="%.3f",
                    disabled=(path_control_mode != "g64_tolerance")
                )
            with pc2:
                g64_naive_cam_q_mm = st.number_input(
                    "Naive CAM G64 Q, мм", min_value=0.0, max_value=10.0,
                    value=0.0, step=0.01, format="%.3f",
                    disabled=(path_control_mode != "g64_tolerance"),
                    help="0 отключает упрощение коротких сегментов. Это безопаснее для короткого перехода C+Z."
                )
            analog_ru = st.selectbox(
                "Команды E0/E2 в непрерывном C-режиме",
                ["M68 — текущая совместимая схема", "M67 — синхронно со следующим движением"],
                index=0,
                help="M67 используйте только после проверки HAL. По умолчанию приложение сохраняет существующую M68-схему вашей установки."
            )
            analog_output_mode = "m67_synchronized" if analog_ru.startswith("M67") else "m68_compatible"
            machine_m67_confirmed = st.checkbox(
                "Подтверждаю поддержку M67 в HAL", value=False,
                disabled=(analog_output_mode != "m67_synchronized"),
                help="Без подтверждения генератор автоматически вернётся к M68, чтобы не выдавать неподдерживаемые команды."
            )
            rotary_c_radius_variation_tolerance_mm = st.number_input(
                "Допуск изменения радиуса в no-pause, мм", min_value=0.0, max_value=10.0,
                value=0.05, step=0.01, format="%.3f",
                help="Если радиус по высоте меняется сильнее, фиксированный X даст неверную чашу/баллон, поэтому генерация блокируется."
            )
            if path_control_mode in ("g61", "g61_1") and rotary_c_motion_mode == "no_pause_flat_rings":
                st.warning("G61/G61.1 конфликтует с целью C без остановок: на границах G-code возможны сильные замедления или остановка.")
            if analog_output_mode == "m67_synchronized" and not machine_m67_confirmed:
                st.info("M67 выбран, но не подтверждён: итоговый G-code безопасно останется на M68.")
            st.info("Внешние overrides не блокируются и не сбрасываются: приложение не вставляет M49/M50. WIRE override 100% — только рекомендация для воспроизводимой калибровки; регулировка остаётся доступной оператору.")

        st.header("10. Целевое время")
        st.session_state.setdefault("w_tt_adv", False)
        target_time_enabled = st.checkbox("Задать желаемое время изготовления", key="w_tt_adv", help="Программа подстроит РАЗРЕШЁННЫЕ параметры и честно покажет, реально ли уложиться в выбранное время.")
        col_th, col_tm = st.columns(2)
        with col_th:
            target_hours = st.number_input("Цель, часы", min_value=0, max_value=200, value=0, step=1, disabled=not target_time_enabled)
        with col_tm:
            target_minutes = st.number_input("Цель, минуты", min_value=0, max_value=59, value=30, step=5, disabled=not target_time_enabled)
        target_fit_mode_ru = st.selectbox(
            "Базовый режим подстройки",
            ["Только скорость F", "Скорость + Z-шаг + шаг дорожек", "Все необходимые параметры (экспериментально)"],
            index=2,
            disabled=not target_time_enabled,
            help="Задаёт стартовый набор. Ниже можно точно выбрать, какие рычаги разрешено крутить и их жёсткие пределы."
        )
        if target_fit_mode_ru.startswith("Только"):
            target_fit_mode = "feed_only"
        elif target_fit_mode_ru.startswith("Скорость"):
            target_fit_mode = "feed_layer_hatch"
        else:
            target_fit_mode = "full_process"
        target_total_s = (int(target_hours) * 3600 + int(target_minutes) * 60) if target_time_enabled else 0

        # v4.2.9.31: selectable regulation levers. Each lever: allow to change + hard limit.
        # E2 (wire) is ALWAYS dependent (computed from geometry x speed) to prevent over-feed.
        time_levers = None
        if target_time_enabled:
            st.markdown("**Разрешённые рычаги регулировки времени** (что программе можно менять)")
            st.caption("Скорость проволоки E2 всегда считается из объёма (высота × шаг × скорость), "
                       "чтобы не было переподачи. Отметьте, что разрешено крутить, и задайте жёсткие пределы. "
                       "Зафиксированное не трогается; если разрешённого не хватит — программа честно скажет минимум.")
            lc1, lc2 = st.columns(2)
            with lc1:
                lever_layer_on = st.checkbox("Высота слоя", value=True, help="Выше слой → меньше слоёв → быстрее. Влияет на качество поверхности.")
                lever_layer_max = st.number_input("макс. высота слоя, мм", min_value=0.1, max_value=5.0, value=1.5, step=0.1, disabled=not lever_layer_on, format="%.2f")
                lever_step_on = st.checkbox("Шаг колец (ширина)", value=True, help="Шире шаг → меньше колец → быстрее. Слишком широко → непроплав между кольцами.")
                lever_step_max = st.number_input("макс. шаг колец, мм", min_value=0.3, max_value=10.0, value=4.0, step=0.1, disabled=not lever_step_on, format="%.2f")
            with lc2:
                lever_cspeed_on = st.checkbox("Скорость стола C", value=True, help="Быстрее вращение → быстрее кольцо. Ограничено паспортом привода.")
                lever_cspeed_max = st.number_input("макс. скорость C, град/мин", min_value=50.0, max_value=5000.0, value=600.0, step=50.0, disabled=not lever_cspeed_on, format="%.0f")
                st.caption("⚠️ Рычаг скорости C действует только для стратегий поворотного стола; для XY-стратегий игнорируется.")
                lever_current_on = st.checkbox("Ток пучка E0", value=False, help="Выше ток → можно быстрее с проплавом. Задаётся оператором по возможностям установки.")
                lever_current_max = st.number_input("макс. ток E0, мА", min_value=1.0, max_value=200.0, value=50.0, step=1.0, disabled=not lever_current_on, format="%.0f")
            time_levers = {
                "layer_height": {"enabled": bool(lever_layer_on), "max": float(lever_layer_max)},
                "radial_step": {"enabled": bool(lever_step_on), "max": float(lever_step_max)},
                "c_speed": {"enabled": bool(lever_cspeed_on), "max": float(lever_cspeed_max)},
                "current": {"enabled": bool(lever_current_on), "max": float(lever_current_max)},
            }

    settings = ProcessSettings(
        voltage_kv=voltage,
        layer_height=layer_height,
        hatch_spacing=hatch_spacing,
        wire_diameter_mm=wire_d,
        deposition_efficiency=float(deposition_efficiency),
        wire_min_mm_s=wire_min,
        wire_max_mm_s=wire_max,
        density_g_cm3=density,
        direction=direction,
        center_xy=center_xy,
        target_energy_bottom_j_per_mm=e0,
        target_energy_top_j_per_mm=e1,
        feed_bottom_mm_min=f0,
        feed_top_mm_min=f1,
        focus_ma=focus,
        current_min_ma=current_min,
        current_low_warning_ma=current_low_warning,
        current_max_ma=current_max,
        min_beam_power_w=float(min_beam_power_w),
        power_floor_warning_enabled=bool(power_floor_warning_enabled),
        beam_current_mode=beam_current_mode,
        beam_current_bottom_ma=float(beam_current_bottom_ma),
        beam_current_top_ma=float(beam_current_top_ma),
        z_hop_mm=zhop,
        safe_initial_approach_enabled=bool(safe_initial_approach_enabled),
        safe_initial_approach_z_mm=float(safe_initial_approach_z_mm),
        use_w_retract=use_w,
        w_retract_mm=wret,
        edge_offset=edge_offset,
        contour_passes=int(contour_passes),
        contour_every_n_layers=int(contour_every),
        contour_first=contour_first,
        thermal_ordering=thermal_ordering,
        lead_in_beam_mm=lead,
        soft_start_mm=soft_start,
        soft_finish_mm=soft_finish,
        adaptive_thin_wall=adaptive,
        force_contour_on_empty_layers=adaptive,
        adaptive_section_probe=adaptive_probe,
        adaptive_wire_correction=adaptive_wire,
        manual_section_fallback=manual_fallback,
        projection_fallback_if_empty=projection_fallback,
        deposition_strategy=deposition_strategy,
        rotational_path_strategy=rotational_path_strategy,
        rotational_radial_step_mm=float(rotational_radial_step_mm),
        rotational_points_per_circle=int(rotational_points_per_circle),
        rotary_c_center_x_mm=float(rotary_c_center_x_mm),
        rotary_c_center_y_mm=float(rotary_c_center_y_mm),
        rotary_c_direction=str(rotary_c_direction),
        rotary_c_start_deg=float(rotary_c_start_deg),
        rotary_c_b_angle_deg=float(rotary_c_b_angle_deg),
        rotary_c_max_deg_min=float(rotary_c_max_deg_min),
        rotary_c_min_radius_mm=float(rotary_c_min_radius_mm),
        rotary_c_auto_limit_feed=bool(rotary_c_auto_limit_feed),
        rotary_c_seam_scatter_deg=float(rotary_c_seam_scatter_deg),
        rotary_c_motion_mode=str(rotary_c_motion_mode),
        rotary_c_transition_angle_deg=float(rotary_c_transition_angle_deg),
        rotary_c_continuous_keep_beam_wire_on=bool(rotary_c_continuous_keep_beam_wire_on),
        rotary_c_disable_layer_pauses=bool(rotary_c_disable_layer_pauses),
        rotary_c_disable_w_retract=bool(rotary_c_disable_w_retract),
        rotary_c_disable_z_hop=bool(rotary_c_disable_z_hop),
        rotary_c_radius_variation_tolerance_mm=float(rotary_c_radius_variation_tolerance_mm),
        path_control_mode=str(path_control_mode),
        g64_tolerance_mm=float(g64_tolerance),
        g64_naive_cam_q_mm=float(g64_naive_cam_q_mm),
        analog_output_mode=str(analog_output_mode),
        machine_m67_confirmed=bool(machine_m67_confirmed),
        wire_feed_mode=str(wire_feed_mode),
        wire_feed_manual_mm_s=float(wire_feed_manual_mm_s),
        wire_feed_bottom_mm_s=float(wire_feed_bottom_mm_s),
        wire_feed_top_mm_s=float(wire_feed_top_mm_s),
        alternate_layer_rotation=alternate_layer_rotation,
        link_feed_factor=float(link_feed_factor),
        bead_width_mm=float(bead_width_mm),
        overlap_model=overlap_model,
        auto_hatch_from_bead=bool(auto_hatch_from_bead),
        include_comments=include_comments,
    )
    time_plan = {"enabled": False, "messages": [], "target_s": 0.0, "base_total_s": 0.0, "adjusted_total_s": 0.0, "possible": True, "severity": "ok"}
    if target_time_enabled and target_total_s > 0:
        settings, time_plan = _fit_settings_to_target_time(summary, settings, float(target_total_s), target_fit_mode, time_levers)
    _cv_apply = st.session_state.get("_cv_settings_active") or {}
    if _cv_apply.get("rotary_c_constant_velocity"):
        settings = replace(settings, **_cv_apply)
    _th_apply = st.session_state.get("_thermal_settings_active") or {}
    if _th_apply.get("thermal_min_layer_cycle_enabled"):
        settings = replace(settings, **_th_apply)
    _rp_apply = st.session_state.get("_ramp_settings_active") or {}
    if _rp_apply.get("simplify_wire_ramps"):
        settings = replace(settings, **_rp_apply)
    settings, c_limit_info = _apply_rotary_c_feed_limit_to_settings(summary, settings)
    if c_limit_info:
        time_plan["c_limit_info"] = c_limit_info
        with st.sidebar:
            if c_limit_info.get("limited"):
                st.warning(c_limit_info["operator_message"])
            else:
                st.info(c_limit_info["operator_message"])
    return settings, time_plan


def _adaptive_preview_segments(polys, settings, layer_index: int):
    """Build preview paths using the same thin-wall idea as G-code generation.

    In v2.9 the preview used only the normal hatch settings. For thin rings or
    STL sections this could show an empty picture even when generation later
    recovered the layer adaptively. v3.0 preview now follows the same fallback
    path: normal hatch -> thinner hatch -> contour fallback.
    """
    segs = _hatch_segments_for_polygons(polys, settings, layer_index)
    cont = _contour_segments_for_polygons(polys, settings, layer_index)
    preview_note = "обычный режим"

    if settings.adaptive_thin_wall and not segs:
        thin_settings = replace(
            settings,
            edge_offset=max(0.0, min(settings.edge_offset * settings.thin_wall_edge_offset_factor, settings.edge_offset, 0.35)),
            hatch_spacing=max(0.35, min(settings.hatch_spacing * settings.thin_wall_hatch_spacing_factor, settings.hatch_spacing)),
            min_segment_length=max(0.25, min(settings.thin_wall_min_segment_length, settings.min_segment_length)),
        )
        segs_retry = _hatch_segments_for_polygons(polys, thin_settings, layer_index)
        if segs_retry:
            segs = segs_retry
            preview_note = "адаптивная тонкая штриховка"

    if settings.adaptive_thin_wall and settings.force_contour_on_empty_layers and not segs and not cont:
        contour_settings = replace(
            settings,
            contour_passes=max(1, settings.contour_passes),
            edge_offset=max(0.0, min(settings.edge_offset * settings.thin_wall_edge_offset_factor, settings.edge_offset, 0.35)),
            min_segment_length=max(0.25, min(settings.thin_wall_min_segment_length, settings.min_segment_length)),
        )
        cont_retry = _contour_segments_for_polygons(polys, contour_settings, layer_index)
        if cont_retry:
            cont = cont_retry
            preview_note = "контурный резервный режим для тонкого слоя"

    return segs, cont, preview_note



def _ordered_preview_with_links(segs, settings):
    """Preview hatch in the exact visible style of selected path plan.

    Returns displayed work segments and dashed link moves for continuous snake.
    Segment geometry alone can look identical for Y-/Y+ or X-/X+, therefore
    the preview also draws arrows so the operator can see the direction.
    """
    if not segs:
        return [], []
    continuous = str(getattr(settings, "deposition_strategy", "continuous")).strip().lower() == "continuous"
    direction = str(getattr(settings, "direction", "Y-")).upper()
    axis_y = direction.startswith("Y")
    if not continuous:
        return list(segs), []
    ordered = sorted(segs, key=(lambda s: s[0]) if axis_y else (lambda s: s[1]))
    zigzag = []
    links = []
    prev_end = None
    for i, s in enumerate(ordered):
        x0, y0, x1, y1 = s
        # honour the first direction exactly; neighbours reverse automatically
        if i % 2 == 1:
            seg = (x1, y1, x0, y0)
        else:
            seg = (x0, y0, x1, y1)
        if prev_end is not None:
            sx, sy = seg[0], seg[1]
            if math.hypot(sx - prev_end[0], sy - prev_end[1]) > 1e-6:
                links.append((prev_end[0], prev_end[1], sx, sy))
        zigzag.append(seg)
        prev_end = (seg[2], seg[3])
    return zigzag, links


def _draw_direction_arrows(ax, segs, limit: int = 40):
    """Add small arrows so X+/X-/Y+/Y- actually changes the preview visually."""
    if not segs:
        return
    step = max(1, len(segs) // max(1, limit))
    for s in list(segs)[::step][:limit]:
        x0, y0, x1, y1 = s
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        # short arrow centered on the segment
        mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        scale = min(0.22, 4.0 / max(length, 1e-9))
        ax.annotate(
            "",
            xy=(mx + dx * scale, my + dy * scale),
            xytext=(mx - dx * scale, my - dy * scale),
            arrowprops=dict(arrowstyle="->", linewidth=0.7),
        )


def _path_plan_label_from_settings(settings) -> str:
    direction = str(getattr(settings, "direction", "Y-")).upper()
    continuous = str(getattr(settings, "deposition_strategy", "continuous")).strip().lower() == "continuous"
    if continuous:
        return f"Змейка/непрерывно: первый проход {direction}, соседние дорожки разворачиваются; пунктир = перемычка без E2."
    return f"Параллельно/посегментно: все рабочие проходы {direction}; перемычек змейки нет."

def draw_preview_from_polys(polys, settings, layer_index=1, title="Предпросмотр"):
    if not polys:
        st.warning("На выбранной высоте сечение STL не найдено. Попробуйте другой Z или включите устойчивый поиск STL-сечения.")
        return
    segs, cont, note = _adaptive_preview_segments(polys, settings, layer_index)
    show_segs, link_segs = _ordered_preview_with_links(segs, settings)
    fig, ax = plt.subplots(figsize=(7, 5))
    for p in polys:
        x, y = p.exterior.xy
        ax.plot(x, y, linewidth=1.5)
        for hole in p.interiors:
            hx, hy = hole.xy
            ax.plot(hx, hy, linewidth=1.0)
    for s in show_segs[:1200]:
        ax.plot([s[0], s[2]], [s[1], s[3]], linewidth=0.7)
    for s in link_segs[:1200]:
        ax.plot([s[0], s[2]], [s[1], s[3]], linewidth=0.65, linestyle="--")
    for s in cont[:800]:
        ax.plot([s[0], s[2]], [s[1], s[3]], linewidth=1.0, linestyle=":")
    _draw_direction_arrows(ax, show_segs, limit=45)
    ax.set_aspect('equal', 'box')
    ax.set_title(f"{title}: рабочих дорожек={len(show_segs)}, перемычек={len(link_segs)}, контуров={len(cont)}")
    ax.set_xlabel("X, мм")
    ax.set_ylabel("Y, мм")
    if not show_segs and not cont:
        ax.text(0.5, 0.5, "На этом сечении нет траекторий", ha="center", va="center", transform=ax.transAxes)
        st.warning("Сечение найдено, но траектории не построились. Уменьшите отступ от края, шаг дорожек или минимальную длину сегмента.")
    else:
        st.caption(f"Предпросмотр: {_path_plan_label_from_settings(settings)} {note}. Показано: рабочих дорожек={len(show_segs)}, перемычек={len(link_segs)}, контуров={len(cont)}.")
    st.pyplot(fig)
    plt.close(fig)


def draw_rotational_path_preview(polys, segs, settings, title="Кольцевая/спиральная траектория"):
    fig, ax = plt.subplots(figsize=(7, 5))
    for p in polys or []:
        try:
            x, y = p.exterior.xy
            ax.plot(x, y, linewidth=1.5)
            for hole in p.interiors:
                hx, hy = hole.xy
                ax.plot(hx, hy, linewidth=1.0)
        except Exception:
            pass
    for s in segs[:2500]:
        ax.plot([s[0], s[2]], [s[1], s[3]], linewidth=0.65)
    ax.set_aspect('equal', 'box')
    ax.set_title(f"{title}: сегментов={len(segs)}")
    ax.set_xlabel("X, мм")
    ax.set_ylabel("Y, мм")
    if not segs:
        st.warning("Кольцевая/спиральная траектория не построилась. Проверьте диаметр, стенку и радиальный шаг.")
    else:
        st.caption(f"Показана специализированная траектория чаши/баллона: {_rotational_strategy_ru(settings.rotational_path_strategy)}, сегментов={len(segs)}.")
    st.pyplot(fig)
    plt.close(fig)



def _reverse_segments_dataframe(segments):
    rows = []
    for s in segments:
        rows.append({
            "строка": s.get("line"),
            "тип": s.get("type"),
            "x1": round(s.get("x1", 0.0), 3),
            "y1": round(s.get("y1", 0.0), 3),
            "z1": round(s.get("z1", 0.0), 3),
            "x2": round(s.get("x2", 0.0), 3),
            "y2": round(s.get("y2", 0.0), 3),
            "z2": round(s.get("z2", 0.0), 3),
            "длина, мм": round(s.get("length_mm", 0.0), 3),
            "F, мм/мин": s.get("feed_mm_min"),
            "E0_mA": s.get("current_ma"),
            "E2_mm_s": s.get("wire_mm_s"),
        })
    return pd.DataFrame(rows)



def _circle_segments_for_preview(cx: float, cy: float, r: float, n: int = 160):
    if r <= 1e-9:
        return []
    n = max(24, int(n))
    pts = []
    for i in range(n + 1):
        a = 2.0 * math.pi * i / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]) for i in range(len(pts)-1)]



def _spiral_segments_for_preview_from_radii(center, radii, settings, layer_index: int = 1):
    if not radii:
        return []
    ro = max(float(r) for r in radii)
    ri = min(float(r) for r in radii) if len(radii) > 1 else 0.0
    pitch = float(getattr(settings, "rotational_radial_step_mm", 0.0)) or float(settings.hatch_spacing)
    pitch = max(pitch, 0.1)
    npts_circle = max(48, min(int(getattr(settings, "rotational_points_per_circle", 160)), 2048))
    r_start = max(ri + pitch * 0.35, 0.35 if ri <= 0.05 else ri + 0.05)
    r_end = max(ro - pitch * 0.35, r_start + 0.1)
    radial_span = max(r_end - r_start, 0.1)
    turns = max(1.0, radial_span / pitch)
    samples = max(80, int(npts_circle * turns))
    theta0 = (layer_index % 16) * (math.pi / 8.0)
    clockwise = (layer_index % 2 == 0)
    pts = []
    for i in range(samples + 1):
        u = i / max(samples, 1)
        r = r_start + radial_span * u
        a = theta0 + (-1.0 if clockwise else 1.0) * (2.0 * math.pi * turns * u)
        pts.append((center[0] + r * math.cos(a), center[1] + r * math.sin(a)))
    return [(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]) for i in range(len(pts)-1)]


def draw_generic_special_path_preview(polys, settings, layer_index=1, title="Специализированная траектория"):
    if not polys:
        st.warning("На выбранной высоте сечение не найдено, специализированную траекторию показать нельзя.")
        return
    try:
        from shapely import unary_union
        u = unary_union(polys)
        minx, miny, maxx, maxy = u.bounds
        center = ((minx + maxx) * 0.5, (miny + maxy) * 0.5)
    except Exception:
        xs, ys = [], []
        for p in polys:
            try:
                for x, y in p.exterior.coords:
                    xs.append(float(x)); ys.append(float(y))
            except Exception:
                pass
        center = ((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5) if xs and ys else (0.0, 0.0)
    radii, rstats = _section_radii_from_polygons_for_rotary_c(polys, center, settings)
    mode = str(getattr(settings, "rotational_path_strategy", "hatch")).strip().lower()
    if mode in ("rotary_c", "rotary_c_rings", "c_rings", "c_table", "stl_rotary_c_rings", "mesh_rotary_c_rings", "generic_rotary_c_rings"):
        return draw_stl_rotary_c_preview(polys, settings, title=title)
    n = int(getattr(settings, "rotational_points_per_circle", 160))
    segs = []
    if mode in ("spiral", "xy_spiral"):
        segs = _spiral_segments_for_preview_from_radii(center, radii, settings, layer_index)
        draw_rotational_path_preview(polys, segs, settings, title=title)
        st.caption("Предпросмотр XY-спирали: показана специализированная спиральная траектория внутри текущего сечения. Для некруглой формы это приближение.")
    elif mode in ("rings", "xy_rings"):
        for r in radii:
            segs.extend(_circle_segments_for_preview(center[0], center[1], r, n))
        draw_rotational_path_preview(polys, segs, settings, title=title)
        st.caption("Предпросмотр концентрических XY-колец: показаны круговые проходы внутри текущего сечения. Для некруглой формы это приближение.")
    else:
        draw_preview_from_polys(polys, settings, layer_index, title=title)
        return
    if radii:
        st.caption(f"Радиусы сечения: {min(radii):.2f}…{max(radii):.2f} мм; центр оценки X={center[0]:.2f}, Y={center[1]:.2f}.")
        if float(rstats.get("roundness_error_pct", 0.0)) > 8.0:
            st.warning(f"Сечение заметно не круглое: разброс радиусов примерно {rstats.get('roundness_error_pct', 0.0):.1f}%. Кольца/спираль будут приближением, не точной траекторией STL.")

def draw_stl_rotary_c_preview(polys, settings, title="STL: поворотный стол C"):
    if not polys:
        st.warning("На выбранной высоте STL-сечение не найдено, C-кольца показать нельзя.")
        return
    try:
        from shapely import unary_union
        u = unary_union(polys)
        minx, miny, maxx, maxy = u.bounds
        center = ((minx + maxx) * 0.5, (miny + maxy) * 0.5)
    except Exception:
        xs, ys = [], []
        for p in polys:
            try:
                for x, y in p.exterior.coords:
                    xs.append(float(x)); ys.append(float(y))
            except Exception:
                pass
        center = ((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5) if xs and ys else (0.0, 0.0)
    radii, rstats = _section_radii_from_polygons_for_rotary_c(polys, center, settings)
    n = int(getattr(settings, "rotational_points_per_circle", 160))
    segs = []
    for r in radii:
        segs.extend(_circle_segments_for_preview(center[0], center[1], r, n))
    draw_rotational_path_preview(polys, segs, settings, title=title)
    if radii:
        req = [rotary_c_speed_deg_min(settings.feed_bottom_mm_min, max(r, 1e-9)) for r in radii]
        st.caption(f"C-предпросмотр: показаны круговые радиусы {min(radii):.2f}…{max(radii):.2f} мм вокруг центра сечения. В G-code они будут выполнены через X=центр C+R и G91 C360. Оценка скорости C при Fниз: {min(req):.0f}…{max(req):.0f} град/мин.")
        if float(rstats.get("roundness_error_pct", 0.0)) > 8.0:
            st.warning(f"Сечение заметно не круглое: оценка разброса радиусов {rstats.get('roundness_error_pct', 0.0):.1f}%. Для такой STL C-режим является приближением, не точным повторением формы.")
    else:
        st.warning("Для этого сечения радиусы C-колец не найдены.")

def draw_reverse_gcode_preview(analysis):
    segments = analysis.get("segments", [])
    if not segments:
        st.warning("В G-code не найдено X/Y/Z-перемещений для построения фигуры.")
        return
    beam_segments = [s for s in segments if s.get("type") in ("deposition", "beam_only")]
    draw_segments = beam_segments or [s for s in segments if s.get("type") != "rapid"] or segments
    col_a, col_b = st.columns([1, 1])
    with col_a:
        st.markdown("### Вид сверху XY")
        fig, ax = plt.subplots(figsize=(7, 5))
        for kind, label, linestyle, width in [
            ("deposition", "наплавка E0+E2", "-", 0.9),
            ("beam_only", "луч без проволоки", "--", 0.8),
            ("wire_only", "проволока без луча", ":", 0.8),
            ("travel", "G1 без E0/E2", "-.", 0.55),
            ("rapid", "G0", ":", 0.4),
        ]:
            data = [s for s in draw_segments if s.get("type") == kind]
            if not data:
                continue
            used_label = False
            for s in data[:6000]:
                ax.plot([s["x1"], s["x2"]], [s["y1"], s["y2"]], linestyle=linestyle, linewidth=width, )
                used_label = True
        ax.set_aspect("equal", "box")
        ax.set_xlabel("X, мм")
        ax.set_ylabel("Y, мм")
        ax.set_title("Восстановленная траектория по G-code")
        st.caption("Линии: сплошные — наплавка E0+E2; пунктир — луч без проволоки; точечные/штриховые — переходы/служебные движения. Легенда с графика убрана, чтобы не перекрывать траекторию.")
        st.pyplot(fig)
        plt.close(fig)
    with col_b:
        st.markdown("### 3D-траектория XYZ")
        try:
            fig = plt.figure(figsize=(7, 5))
            ax = fig.add_subplot(111, projection="3d")
            for kind, label, linestyle, width in [
                ("deposition", "наплавка E0+E2", "-", 0.9),
                ("beam_only", "луч без проволоки", "--", 0.8),
                ("travel", "G1 без E0/E2", "-.", 0.5),
                ("rapid", "G0", ":", 0.4),
            ]:
                data = [s for s in draw_segments if s.get("type") == kind]
                if not data:
                    continue
                used_label = False
                for s in data[:6000]:
                    ax.plot([s["x1"], s["x2"]], [s["y1"], s["y2"]], [s["z1"], s["z2"]], linestyle=linestyle, linewidth=width, )
                    used_label = True
            ax.set_xlabel("X, мм")
            ax.set_ylabel("Y, мм")
            ax.set_zlabel("Z, мм")
            ax.set_title("Z-слои / высота")
            st.caption("3D показывает фактические проходы из G-code. Подписи вынесены в текст, чтобы не перекрывать модель.")
            st.pyplot(fig)
            plt.close(fig)
        except Exception as exc:
            st.warning(f"3D-предпросмотр G-code не построен: {exc}")



def _reverse_w_dataframe(w_moves):
    rows = []
    for w in w_moves:
        rows.append({
            "строка": w.get("line"),
            "движение": w.get("motion"),
            "W было": round(w.get("w_from", 0.0), 4),
            "W стало": round(w.get("w_to", 0.0), 4),
            "dW, мм": round(w.get("dW_mm", 0.0), 4),
            "F, мм/мин": w.get("feed_mm_min"),
            "E0, мА": w.get("current_ma"),
            "E2, мм/с": w.get("wire_mm_s"),
            "G91 относит.?": bool(w.get("relative_mode")),
        })
    return pd.DataFrame(rows)


def render_gcode_reverse_analyzer(default_settings: ProcessSettings | None = None, key_prefix: str = "reverse"):
    """Shared UI for standalone and tab-based reverse G-code analysis."""
    default_settings = default_settings or ProcessSettings()
    st.subheader("Обратный анализ G-code")
    st.caption("Загрузите .ngc/.txt G-code. Приложение восстановит траекторию из G0/G1, определит активные участки E0/E2/W, покажет фигуру и даст краткие рекомендации.")
    st.warning("Это обратный просмотр траектории, а не восстановление исходного STL. Исходную 3D-поверхность из G-code полностью восстановить нельзя, но можно увидеть фактические проходы, слои и опасные места.")

    # Анализ использует безопасные пороги из ProcessSettings.
    # Отдельные поля порогов убраны из интерфейса, чтобы не путать оператора:
    # они ничего не меняли в G-code, а только влияли на предупреждения анализа.
    analysis_settings = default_settings


    gcode_upload = st.file_uploader(
        "Загрузить G-code для анализа",
        type=["ngc", "gcode", "nc", "tap", "txt"],
        key=f"{key_prefix}_gcode_upload",
    )
    if gcode_upload is None:
        st.info("Загрузите готовый .ngc/.gcode/.nc/.tap/.txt файл. В этом режиме STL/DXF/CSV не нужен.")
        return None

    with st.expander("🎞️ Симулятор по слоям (раскраска по QV)", expanded=False):
        try:
            _rawp = gcode_upload.getvalue()
            _txtp = _rawp.decode("utf-8", errors="replace") if isinstance(_rawp, bytes) else str(_rawp)
            _prof = _gtools.layer_ring_profile(_txtp)
            if not _prof["has_data"]:
                st.caption("В этом файле нет кольцевых комментариев (RING …) — симулятор доступен для C-стратегий с комментариями.")
            else:
                _nL = len(_prof["layers"])
                _li = st.slider("Слой", min_value=1, max_value=_nL, value=1, step=1, key=f"{key_prefix}_sim_layer") if _nL > 1 else 1
                _L = _prof["layers"][_li - 1]
                st.caption(f"Слой {_L['layer']}/{_prof['layers'][-1]['total']} · зона {_L['zone']} · "
                           f"Z {_L['z0']:.2f}–{_L['z1']:.2f} мм · колец {len(_L['rings'])}")
                _Rs = [r["R"] for r in _L["rings"]]
                _QVs = [r["qv"] for r in _L["rings"]]
                _fig, _ax = plt.subplots(figsize=(6.4, 6.4))
                # concentric rings coloured by QV: green(safe)->red(fail ~42)
                _vmin, _vmax = 42.0, max(70.0, _prof["qv_max"])
                for _r, _qv in zip(_Rs, _QVs):
                    _frac = max(0.0, min(1.0, (_qv - _vmin) / max(_vmax - _vmin, 1e-9)))
                    _col = (1.0 - _frac, 0.35 + 0.5 * _frac, 0.2)  # red->green
                    _ax.add_patch(plt.Circle((0, 0), _r, fill=False, lw=2.2, color=_col))
                _lim = (max(_Rs) if _Rs else 1.0) * 1.1
                _ax.set_xlim(-_lim, _lim)
                _ax.set_ylim(-_lim, _lim)
                _ax.set_aspect("equal")
                _ax.set_title(f"Кольца слоя {_L['layer']} — цвет = QV (красный ≤42 отказ, зелёный комфорт)")
                _ax.set_xlabel("мм")
                st.pyplot(_fig)
                _wmin = min(_QVs) if _QVs else 0.0
                if _wmin <= 46.5:
                    st.warning(f"На этом слое минимум QV = {_wmin:.1f} Дж/мм³ — близко к порогу отказа (~42). "
                               "Красные кольца — зона, где в поле проволока лезла из ванны.")
                else:
                    st.caption(f"Минимум QV на слое: {_wmin:.1f} Дж/мм³.")
        except Exception as _e:
            st.caption(f"Симулятор недоступен для этого файла: {_e}")

    with st.expander("🔬 Сравнить с другим G-code (diff двух файлов)", expanded=False):
        gcode_upload_b = st.file_uploader(
            "Второй .ngc/.gcode для сравнения",
            type=["ngc", "gcode", "nc", "tap", "txt"],
            key=f"{key_prefix}_gcode_upload_b",
        )
        if gcode_upload_b is not None:
            try:
                _ra = gcode_upload.getvalue()
                _rb = gcode_upload_b.getvalue()
                _ta = _ra.decode("utf-8", errors="replace") if isinstance(_ra, bytes) else str(_ra)
                _tb = _rb.decode("utf-8", errors="replace") if isinstance(_rb, bytes) else str(_rb)
                _d = _gtools.compare_gcode(_ta, _tb, gcode_upload.name, gcode_upload_b.name)
                if _d["identical"]:
                    st.success("Файлы идентичны байт-в-байт (SHA совпал бы). Отличий нет.")
                else:
                    st.caption(f"A = {_d['name_a']}  ·  B = {_d['name_b']}")
                    st.table(pd.DataFrame(_d["rows"]).rename(
                        columns={"field": "Параметр", "a": "A", "b": "B", "delta": "Δ (B−A)"}).set_index("Параметр"))
            except Exception as _e:
                st.warning(f"Не удалось сравнить файлы: {_e}")

    try:
        raw = gcode_upload.getvalue()
        try:
            gcode_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            gcode_text = raw.decode("cp1251", errors="replace")
        analysis = analyze_gcode_reverse(gcode_text, analysis_settings)
        stats = analysis["stats"]

        status_level = stats.get("status_level", "OK")
        status_text = stats.get("status_text", "")
        if status_level == "DANGER":
            st.error(status_text + "\n\nЕсть критичные признаки. Не запускать с лучом/проволокой до исправления и повторной проверки.")
        elif status_level == "WARNING":
            st.warning(status_text + "\n\nКритичных блокирующих ошибок не найдено, но нужен viewer и сухой прогон.")
        else:
            st.success(status_text + "\n\nКритичных признаков по статике не найдено. Всё равно нужен viewer и сухой прогон.")

        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Процесс", stats.get("process_inferred", "—"))
        with m2:
            st.metric("Слоёв, оценка", stats.get("layer_count_estimate", 0))
        with m3:
            st.metric("E0 max, мА", f"{stats.get('max_current_ma', 0.0):.2f}")
        with m4:
            st.metric("E2 max, мм/с", f"{stats.get('max_wire_mm_s', 0.0):.2f}")

        st.markdown("### Краткая расшифровка")
        brief_rows = [
            ("Статус", stats.get("status_text", "—")),
            ("Тип процесса", stats.get("process_inferred", "—")),
            ("Рисунок траектории", stats.get("path_pattern", "—")),
            ("Строк G-code", stats.get("line_count", 0)),
            ("Сегментов всего", stats.get("segment_count", 0)),
            ("Сегментов наплавки E0+E2", stats.get("deposition_segments", 0)),
            ("G0 при активном E0/E2", stats.get("g0_active", 0)),
            ("G90 / G91", f"{stats.get('g90_count', 0)} / {stats.get('g91_count', 0)}"),
            ("W-перемещений", stats.get("w_move_count", 0)),
            ("W при E0 / W при E2", f"{stats.get('w_move_while_beam_on', 0)} / {stats.get('w_move_while_wire_on', 0)}"),
            ("Суммарный |dW|, мм", f"{stats.get('w_total_abs_mm', 0.0):.3f}"),
            ("Диапазон F, мм/мин", f"{stats.get('min_feed_mm_min', 0.0):.1f} ... {stats.get('max_feed_mm_min', 0.0):.1f}"),
            ("Длина активной траектории, мм", f"{stats.get('beam_path_length_mm', 0.0):.1f}"),
            ("Оценка активного времени", _fmt_hm(stats.get("estimated_active_time_s", 0.0))),
            ("Оценка общего времени движений", _fmt_hm(stats.get("estimated_total_motion_time_s", 0.0))),
        ]
        bbox = stats.get("bbox") or {}
        if bbox:
            brief_rows.extend([
                ("Размер X, мм", f"{bbox.get('size_x', 0.0):.3f}"),
                ("Размер Y, мм", f"{bbox.get('size_y', 0.0):.3f}"),
                ("Размер Z, мм", f"{bbox.get('size_z', 0.0):.3f}"),
            ])
        brief_df = pd.DataFrame(brief_rows, columns=["Что", "Значение"])
        brief_df["Значение"] = brief_df["Значение"].map(str)
        st.table(brief_df.set_index("Что"))

        draw_reverse_gcode_preview(analysis)

        if analysis.get("warnings"):
            st.markdown("### Предупреждения и опасности")
            for issue in analysis.get("issues", []):
                msg = issue.get("message", "")
                if issue.get("line"):
                    msg = f"Строка {issue.get('line')}: " + msg
                if issue.get("level") == "DANGER":
                    st.error(msg)
                elif issue.get("level") == "WARNING":
                    st.warning(msg)
                else:
                    st.info(msg)
        else:
            st.info("Предупреждений по статическому анализу не найдено.")

        st.markdown("### Рекомендации")
        for r in analysis.get("recommendations", []):
            st.write("- " + r)

        st.markdown("### W-перемещения")
        w_df = _reverse_w_dataframe(analysis.get("w_moves", []))
        if not w_df.empty:
            st.dataframe(w_df.head(1000), width="stretch", hide_index=True)
        else:
            st.info("W-перемещений не найдено.")

        st.markdown("### Таблица восстановленных сегментов")
        seg_df = _reverse_segments_dataframe(analysis.get("segments", []))
        if not seg_df.empty:
            st.dataframe(seg_df.head(2000), width="stretch", hide_index=True)
        else:
            st.info("Сегментов для таблицы нет.")

        report_text = build_gcode_reverse_report(analysis)
        st.download_button("Скачать отчёт обратного анализа .txt", report_text, file_name=f"ebam_reverse_gcode_analysis_{APP_FILE_TAG}.txt", mime="text/plain", key=f"{key_prefix}_report_download")
        if not seg_df.empty:
            st.download_button("Скачать сегменты траектории .csv", seg_df.to_csv(index=False), file_name=f"ebam_reverse_gcode_segments_{APP_FILE_TAG}.csv", mime="text/csv", key=f"{key_prefix}_segments_download")
        if not w_df.empty:
            st.download_button("Скачать W-перемещения .csv", w_df.to_csv(index=False), file_name=f"ebam_reverse_w_moves_{APP_FILE_TAG}.csv", mime="text/csv", key=f"{key_prefix}_w_download")
        with st.expander("Первые строки отчёта", expanded=False):
            st.text(report_text[:12000])
        return analysis
    except Exception as exc:
        st.error(f"Ошибка обратного анализа G-code: {exc}")
        st.info("Проверьте, что файл содержит обычные команды G0/G1 X/Y/Z/F и, для EBAM, M68 E0/E1/E2.")
        return None


# ------------------------- autonomous mode -------------------------
with st.sidebar:
    st.header("1. Задача")
    app_task = st.radio(
        "Что нужно сделать?",
        ["Создать G-code", "Flange-family R6 (STL → C-кольца)", "Анализ готового G-code", "Калибровка валиков (TEST)"],
        index=0,
        help="Flange-family: параметрический осесимметричный фланец, каждое кольцо изнутри наружу. Анализ: проверить готовый .ngc без геометрии. Калибровка: матрица одиночных валиков.",
    )

if app_task == "Flange-family R6 (STL → C-кольца)":
    from flange_family_generator import render_streamlit_page
    render_streamlit_page()
    st.markdown("---")
    st.caption("By Керенцев Максим")
    st.stop()

if app_task == "Калибровка валиков (TEST)":
    from ebam_gcode_studio.core import generate_calibration_beads, analyze_calibration_results
    st.info("Калибровочная матрица одиночных валиков: строки = токи E0, столбцы = скорости F. "
            "Наплавьте на запасной пластине, измерьте каждый валик (ширина, высота, проплав к плите) "
            "и введите результаты — приложение рассчитает порог мощности проплава, Z-шаг, шаг дорожек и η вашей машины.")
    st.warning("Часть валиков НАМЕРЕННО слабая (для поиска нижней границы). Только запасная пластина. "
               "Перед лучом: viewer → сухой прогон → проверка W. Overrides держать 100% для воспроизводимости (приложение их не трогает).")
    cal_col1, cal_col2 = st.columns(2)
    with cal_col1:
        cal_voltage = st.number_input("Ускоряющее напряжение, кВ", min_value=1.0, max_value=300.0, value=60.0, step=1.0)
        cal_wire_d = st.number_input("Диаметр проволоки, мм", min_value=0.1, max_value=5.0, value=1.2, step=0.1)
        cal_currents_txt = st.text_input("Токи E0, мА (через запятую)", value="8, 12, 16, 20, 24",
                                         help="Строки матрицы. Диапазон вокруг ожидаемого порога: часть не проплавится, часть — проплавится.")
        cal_feeds_txt = st.text_input("Скорости F, мм/мин (через запятую)", value="180, 235, 300",
                                      help="Столбцы матрицы.")
        cal_wire_mode = st.selectbox("Подача проволоки E2", ["Авто из объёма (как в генераторе)", "Фиксированная"], index=0)
        cal_wire_fixed = st.number_input("Фиксированная E2, мм/с", min_value=0.1, max_value=60.0, value=3.5, step=0.1,
                                         disabled=not cal_wire_mode.startswith("Фикс"))
    with cal_col2:
        cal_len = st.number_input("Длина валика, мм", min_value=5.0, max_value=300.0, value=40.0, step=5.0)
        cal_spacing = st.number_input("Шаг между валиками по Y, мм", min_value=3.0, max_value=100.0, value=10.0, step=1.0,
                                      help="Расстояние между строками (токами); заметно больше ожидаемой ширины валика.")
        cal_gap = st.number_input("Зазор между столбцами по X, мм", min_value=3.0, max_value=100.0, value=12.0, step=1.0)
        cal_ox = st.number_input("Начало X, мм", min_value=0.0, max_value=3000.0, value=15.0, step=5.0)
        cal_oy = st.number_input("Начало Y, мм", min_value=0.0, max_value=1400.0, value=15.0, step=5.0)
    try:
        cal_currents = [float(x.replace(",", ".")) for x in cal_currents_txt.replace(";", ",").split(",") if x.strip()]
        cal_feeds = [float(x.replace(",", ".")) for x in cal_feeds_txt.replace(";", ",").split(",") if x.strip()]
    except ValueError:
        cal_currents, cal_feeds = [], []
        st.error("Токи/скорости: не удалось разобрать список чисел. Пример: 8, 12, 16, 20, 24")
    if cal_currents and cal_feeds:
        st.caption(f"Матрица: {len(cal_currents)} токов × {len(cal_feeds)} скоростей = {len(cal_currents)*len(cal_feeds)} валиков. "
                   f"Мощность от {cal_voltage*min(cal_currents):.0f} до {cal_voltage*max(cal_currents):.0f} Вт.")
    if st.button("Сгенерировать G-code матрицы", type="primary", disabled=not (cal_currents and cal_feeds)):
        try:
            cal_settings = ProcessSettings(voltage_kv=float(cal_voltage), wire_diameter_mm=float(cal_wire_d),
                                           current_max_ma=max(max(cal_currents) + 1.0, 50.0), include_comments=True)
            cal_res = generate_calibration_beads(
                cal_settings, currents_ma=cal_currents, feeds_mm_min=cal_feeds,
                bead_length_mm=float(cal_len), bead_spacing_mm=float(cal_spacing), col_gap_mm=float(cal_gap),
                origin_x_mm=float(cal_ox), origin_y_mm=float(cal_oy),
                wire_mode=("fixed" if cal_wire_mode.startswith("Фикс") else "auto"),
                wire_fixed_mm_s=float(cal_wire_fixed))
            st.session_state["cal_result"] = cal_res
            st.session_state["cal_meta"] = {"voltage": float(cal_voltage), "wire_d": float(cal_wire_d)}
        except Exception as exc:
            st.error(f"Не удалось сгенерировать матрицу: {exc}")
    if "cal_result" in st.session_state:
        cal_res = st.session_state["cal_result"]
        st.success(f"Матрица готова: {cal_res.stats['beads_total']} валиков, {cal_res.stats['gcode_lines']} строк G-code. "
                   f"Bormash-лимиты: {'OK' if cal_res.stats.get('bormash_limits_ok') else 'ПРЕВЫШЕНЫ'}.")
        import io as _io, zipfile as _zf
        _buf = _io.BytesIO()
        with _zf.ZipFile(_buf, "w", _zf.ZIP_DEFLATED) as zf:
            zf.writestr("calibration_beads.ngc", cal_res.gcode)
            zf.writestr("calibration_bead_map.csv", cal_res.layer_csv)
            zf.writestr("calibration_audit.txt", cal_res.audit_text)
        st.download_button("Скачать комплект калибровки (ZIP)", data=_buf.getvalue(),
                           file_name="ebam_calibration_beads.zip", mime="application/zip")
        st.markdown("---")
        st.subheader("Результаты измерений")
        st.caption("Измерьте каждый валик: ширина (мм), высота (мм), проплав к плите (да/нет). "
                   "Непроплав = шарики/комки или валик отделяется от плиты.")
        map_lines = cal_res.layer_csv.strip().splitlines()
        cols = map_lines[0].split(",")
        base_rows = [dict(zip(cols, ln.split(","))) for ln in map_lines[1:]]
        df0 = pd.DataFrame([{
            "bead_id": int(r["bead_id"]), "current_ma": float(r["current_ma"]),
            "feed_mm_min": float(r["feed_mm_min"]), "wire_mm_s": float(r["wire_mm_s"]),
            "power_w": float(r["power_w"]), "width_mm": 0.0, "height_mm": 0.0, "fused": False,
        } for r in base_rows])
        edited = st.data_editor(df0, num_rows="fixed", hide_index=True, key="cal_editor",
                                disabled=["bead_id", "current_ma", "feed_mm_min", "wire_mm_s", "power_w"])
        if st.button("Рассчитать рекомендации по измерениям"):
            meta = st.session_state.get("cal_meta", {"voltage": 60.0, "wire_d": 1.2})
            rows = edited.to_dict("records")
            used = [r for r in rows if r.get("fused") or float(r.get("width_mm") or 0) > 0 or float(r.get("height_mm") or 0) > 0]
            if not used:
                st.error("Не заполнено ни одного валика: отметьте проплав и/или введите размеры хотя бы для части валиков.")
            else:
                ana = analyze_calibration_results(rows, voltage_kv=meta["voltage"], wire_diameter_mm=meta["wire_d"])
                if ana.get("error"):
                    st.error(ana["error"])
                else:
                    m1, m2, m3 = st.columns(3)
                    if ana.get("recommended_min_beam_power_w"):
                        m1.metric("Порог мощности проплава", f"{ana['recommended_min_beam_power_w']:.0f} Вт",
                                  help="Вставьте в 'Порог мощности проплава' в расширенном режиме создания G-code.")
                    if ana.get("recommended_z_step_mm"):
                        m2.metric("Z-шаг (медианная высота валика)", f"{ana['recommended_z_step_mm']:.2f} мм")
                    if ana.get("recommended_hatch_tom_mm"):
                        m3.metric("Шаг дорожек TOM (0.738·w)", f"{ana['recommended_hatch_tom_mm']:.2f} мм",
                                  help=f"FOM (0.667·w): {ana.get('recommended_hatch_fom_mm', 0):.2f} мм")
                    m4, m5, m6 = st.columns(3)
                    if ana.get("recommended_energy_j_mm"):
                        m4.metric("Целевая энергия (медиана проплава)", f"{ana['recommended_energy_j_mm']:.0f} Дж/мм")
                    if ana.get("deposition_efficiency_estimate"):
                        m5.metric("η (оценка)", f"{ana['deposition_efficiency_estimate']:.2f}",
                                  help="Коэффициент осаждения из измеренных валиков; сверить с массовой калибровкой.")
                    m6.metric("Валиков учтено", f"{ana['beads_used']} (проплав {ana.get('fused_count',0)} / нет {ana.get('unfused_count',0)})")
                    if ana.get("power_unfused_max_w") and ana.get("power_fused_min_w"):
                        st.info(f"Граница проплава по вашим данным: непроплав до {ana['power_unfused_max_w']:.0f} Вт, "
                                f"проплав от {ana['power_fused_min_w']:.0f} Вт. Для уточнения добавьте валики между этими мощностями.")
                    for w in ana.get("warnings", []):
                        st.warning(w)
                    st.caption("Рекомендации — стартовые значения для короткого TEST, не гарантированный режим. "
                               "Перенесите порог/энергию/Z-шаг/шаг дорожек в 'Создать G-code' вручную.")
                    if ana.get("deposition_efficiency_estimate"):
                        st.session_state["cal_eta_beads"] = float(ana["deposition_efficiency_estimate"])
    with st.expander("Сверка η: валики ↔ реальная деталь"):
        from ebam_gcode_studio.core import estimate_eta_from_wall, eta_cross_check
        st.caption("Две независимые оценки коэффициента осаждения должны сходиться: из матрицы валиков и из фактической стенки детали. Расхождение — диагностика (потери, ошибка измерения, разные тепловые режимы).")
        ec1, ec2 = st.columns(2)
        with ec1:
            x_wall = st.number_input("Фактическая стенка детали, мм", min_value=0.0, max_value=100.0, value=0.0, step=0.1, key="etax_wall")
            x_z = st.number_input("Z-шаг детали, мм", min_value=0.01, max_value=20.0, value=0.5, step=0.05, key="etax_z")
            x_f = st.number_input("Линейная скорость детали, мм/мин", min_value=1.0, max_value=20000.0, value=700.0, step=10.0, key="etax_f")
        with ec2:
            x_e2 = st.number_input("E2 детали, мм/с", min_value=0.01, max_value=60.0, value=5.5, step=0.05, key="etax_e2")
            x_d = st.number_input("Диаметр проволоки, мм", min_value=0.1, max_value=5.0, value=1.2, step=0.1, key="etax_d")
            x_beads = st.number_input("η из валиков (авто из анализа выше, можно править)",
                                      min_value=0.0, max_value=1.5,
                                      value=float(st.session_state.get("cal_eta_beads", 0.0)), step=0.01, key="etax_beads")
        if st.button("Сверить η", key="etax_btn"):
            eta_part = estimate_eta_from_wall(x_wall, x_z, x_f, x_e2, x_d)
            if eta_part is None:
                st.error("η по детали не рассчитано: проверьте, что стенка, Z-шаг, скорость и E2 больше нуля.")
            else:
                st.write(f"η по детали (масса/стенка): **{eta_part:.2f}**")
                res_cc = eta_cross_check(x_beads if x_beads > 0 else None, eta_part)
                if res_cc["verdict"] == "agree":
                    st.success(res_cc["message"])
                elif res_cc["verdict"] == "disagree":
                    st.warning(res_cc["message"])
                else:
                    st.info(res_cc["message"])
    with st.expander("Протокол квалификации M67 по HAL (dry run)"):
        from ebam_gcode_studio.core import generate_m67_hal_check_kit
        st.caption("Комплект для подтверждения синхронного M67 на реальном HAL: dry-run G-code + чек-лист. "
                   "ТОЛЬКО сухой прогон: HV выключен, привод проволоки отключён. Пока чек-лист не пройден, "
                   "приложение продолжает выдавать M68 (гейт machine_m67_confirmed не трогается автоматически).")
        if st.button("Сформировать комплект M67", key="m67kit_btn"):
            kit = generate_m67_hal_check_kit(ProcessSettings())
            st.session_state["m67_kit"] = kit
        if "m67_kit" in st.session_state:
            kit = st.session_state["m67_kit"]
            import io as _io2, zipfile as _zf2
            _b2 = _io2.BytesIO()
            with _zf2.ZipFile(_b2, "w", _zf2.ZIP_DEFLATED) as zf2:
                zf2.writestr("m67_hal_check_DRYRUN.ngc", kit["gcode"])
                zf2.writestr("m67_hal_checklist_RU.txt", kit["checklist"])
            st.download_button("Скачать комплект M67 (ZIP)", data=_b2.getvalue(),
                               file_name="ebam_m67_hal_check_kit.zip", mime="application/zip", key="m67kit_dl")
            st.code(kit["checklist"], language=None)
    st.markdown("---")
    st.caption("By Керенцев Максим")
    st.stop()

if app_task == "Анализ готового G-code":
    st.info("Анализатор G-code: геометрию загружать не нужно.")
    render_gcode_reverse_analyzer(ProcessSettings(), key_prefix="standalone_reverse")
    st.markdown("---")
    st.caption("By Керенцев Максим")
    st.stop()

# ------------------------- sidebar source -------------------------
with st.sidebar:
    st.header("2. Режим работы")
    ui_mode_ru = st.radio("Интерфейс", ["Простой", "Расширенный"], index=0, horizontal=True)
    advanced_mode = ui_mode_ru == "Расширенный"
    if advanced_mode:
        st.caption("Расширенный режим: доступны все технологические настройки и диагностика.")
    else:
        st.caption("Простой режим: выбери геометрию, материал и цель — остальное рассчитывается автоматически.")
    if st.button("Очистить кэш геометрии", help="Полезно, если меняли файлы с тем же именем или хотите освободить память."):
        st.cache_data.clear()
        st.rerun()
    st.divider()

    st.header("3. Геометрия")
    src_type = st.selectbox("Источник", ["Чаши/баллоны", "STL 3D", "Стандартная фигура", "DXF 2D + высота", "CSV X,Y + высота"], index=0)
    st.header("4. Материал и цель")
    mat_labels = {k: v["name_ru"] for k, v in MATERIAL_LIBRARY.items()}
    material_key = st.selectbox("Материал", list(mat_labels.keys()), format_func=lambda k: mat_labels[k], index=0)
    st.caption(MATERIAL_LIBRARY[material_key].get("note_ru", ""))
    if not bool(MATERIAL_LIBRARY[material_key].get("field_calibrated", False)):
        _mat_d = float(MATERIAL_LIBRARY[material_key].get("wire_diameter_mm", 1.2))
        _e2_beam = max_wire_feed_for_beam(_mat_d, 40.0)
        st.info(
            f"Расчётный потолок подачи для Ø{_mat_d:.1f} мм при токе 40 мА: **{_e2_beam:.1f} мм/с** "
            f"(сечение проволоки {wire_area_from_diameter(_mat_d):.3f} мм²). Ограничение задаёт ЛУЧ, а не механизм: "
            f"чем толще проволока, тем меньше её метров в секунду он успевает расплавить."
        )
        st.warning(
            f"Профиль для проволоки {_mat_d:.1f} мм НЕ подтверждён наплавками на этой машине. "
            "Плотность энергии QV взята от проверенной проволоки 1.2 мм, а всё, что зависит от диаметра "
            "(сечение проволоки, подача, пределы слоя), пересчитано автоматически. "
            "С ростом диаметра растёт теплоёмкость входящего металла и инерция расплавления, поэтому "
            "реальная QV может потребоваться выше. Обязательно наплавьте калибровочный валик "
            "(вкладка «Калибровка») и сверьте ширину/высоту с расчётом, прежде чем делать деталь."
        )
    mode = st.selectbox("Цель", ["Качество", "Баланс", "Скорость"], index=1, help="Качество — аккуратнее и медленнее; Баланс — стартовый режим; Скорость — быстрее, но выше риск ухудшения поверхности и формы.")

# ------------------------- load geometry -------------------------
raw_mesh = None
mesh = None
polys = None
summary = None
height_2d = None
source_info = ""
rotational_params = None


if src_type == "Чаши/баллоны":
    with st.sidebar:
        st.header("5. Чаша / шар-баллон")
        vessel_ru = st.selectbox(
            "Тип фигуры",
            ["Шар-баллон с горловиной", "Чаша/горшок", "Стакан/цилиндр", "Конусная чаша"],
            index=0,
            help="Параметрический режим без STL: программа сама создаёт послойные кольцевые/оболочечные сечения."
        )
        height_v = st.number_input("Высота Z, мм", min_value=10.0, max_value=1000.0, value=100.0, step=5.0)
        max_d = st.number_input("Максимальный диаметр, мм", min_value=10.0, max_value=1500.0, value=120.0, step=5.0)
        wall_v = st.number_input("Толщина стенки, мм", min_value=0.5, max_value=50.0, value=5.0, step=0.5)
        bottom_solid = st.number_input("Толщина закрытого донца, мм", min_value=0.0, max_value=100.0, value=max(2.0, wall_v), step=0.5)
        if vessel_ru == "Шар-баллон с горловиной":
            profile_type = "sphere_balloon"
            bottom_d = st.number_input("Диаметр у донца, мм", min_value=1.0, max_value=max_d, value=min(40.0, max_d*0.35), step=2.0)
            top_d = st.number_input("Диаметр горловины/отверстия сверху, мм", min_value=1.0, max_value=max_d, value=min(45.0, max_d*0.38), step=2.0)
            peak_z = st.number_input("Где максимальный диаметр по высоте, доля 0..1", min_value=0.15, max_value=0.85, value=0.55, step=0.05)
            bulge = st.number_input("Плавность раздува", min_value=0.2, max_value=4.0, value=1.0, step=0.1)
            st.warning("Шар-баллон имеет сильные наклоны стенки в верхней зоне. Для первого опыта делайте TEST 10–20 мм и не закрывайте шар полностью без отдельной стратегии/оснастки.")
        elif vessel_ru == "Чаша/горшок":
            profile_type = "bowl"
            bottom_d = st.number_input("Диаметр дна, мм", min_value=1.0, max_value=max_d, value=min(60.0, max_d*0.55), step=2.0)
            top_d = st.number_input("Диаметр раскрытия сверху, мм", min_value=1.0, max_value=1500.0, value=max_d, step=5.0)
            peak_z = 0.55
            bulge = st.number_input("Кривизна стенки", min_value=0.3, max_value=3.0, value=0.85, step=0.05)
        elif vessel_ru == "Стакан/цилиндр":
            profile_type = "straight_cup"
            bottom_d = max_d
            top_d = max_d
            peak_z = 0.5
            bulge = 1.0
        else:
            profile_type = "cone"
            bottom_d = st.number_input("Нижний диаметр, мм", min_value=1.0, max_value=max_d, value=min(60.0, max_d*0.55), step=2.0)
            top_d = max_d
            peak_z = 0.5
            bulge = 1.0
        res_v = st.number_input("Разрешение окружности", min_value=48, max_value=512, value=192, step=16)
        rotational_params = {
            "profile_type": profile_type,
            "height_mm": float(height_v),
            "bottom_diameter_mm": float(bottom_d),
            "top_diameter_mm": float(top_d),
            "max_diameter_mm": float(max_d),
            "wall_thickness_mm": float(wall_v),
            "bottom_solid_mm": float(bottom_solid),
            "peak_z_fraction": float(peak_z),
            "bulge": float(bulge),
            "resolution": int(res_v),
        }
        st.caption("Рекомендация: для первой демонстрации — высота 80–120 мм, диаметр 100–150 мм, стенка 4–6 мм, TEST до Z10–20 мм.")
    base_summary = rotational_shell_summary(rotational_params)
    settings, time_plan = build_settings(base_summary, mode, material_key, advanced_mode=advanced_mode)
    summary = rotational_shell_summary(rotational_params)
    source_info = "Чаши/баллоны: " + vessel_ru
    height_2d = float(summary["size_z"])
    polys = rotational_shell_polygons_at_z(height_2d * 0.5, rotational_params)

elif src_type == "Стандартная фигура":
    with st.sidebar:
        st.header("5. Стандартная фигура")
        shape_ru = st.selectbox(
            "Фигура",
            ["Прямоугольник", "Квадрат", "Круг", "Кольцо", "Эллипс", "Треугольник", "Многоугольник", "Звезда", "Капсула/овал"],
            index=0,
        )
        height_2d = st.number_input("Высота построения Z, мм", min_value=0.1, max_value=1000.0, value=100.0, step=1.0)
        shape_map = {
            "Прямоугольник": "rectangle",
            "Квадрат": "square",
            "Круг": "circle",
            "Кольцо": "ring",
            "Эллипс": "ellipse",
            "Треугольник": "triangle",
            "Многоугольник": "regular_polygon",
            "Звезда": "star",
            "Капсула/овал": "capsule",
        }
        params = {"resolution": 128}
        if shape_ru == "Прямоугольник":
            params["width_x"] = st.number_input("Ширина X, мм", min_value=0.1, max_value=2000.0, value=20.0, step=1.0)
            params["length_y"] = st.number_input("Длина Y, мм", min_value=0.1, max_value=2000.0, value=100.0, step=1.0)
            st.caption("Прямоугольник удобен для стенок и пластин. X — ширина набора дорожек, Y — длина рабочего хода.")
        elif shape_ru == "Квадрат":
            params["side"] = st.number_input("Сторона, мм", min_value=0.1, max_value=2000.0, value=50.0, step=1.0)
            st.caption("Квадрат — частный случай прямоугольника с одинаковыми X и Y.")
        elif shape_ru == "Круг":
            params["radius"] = st.number_input("Радиус, мм", min_value=0.1, max_value=1000.0, value=30.0, step=1.0)
            st.caption("Круг формируется штриховкой внутри круглого сечения; для тонкой стенки лучше выбирать Кольцо.")
        elif shape_ru == "Кольцо":
            outer_r = st.number_input("Внешний радиус, мм", min_value=0.1, max_value=1000.0, value=40.0, step=1.0)
            params["outer_radius"] = outer_r
            params["inner_radius"] = st.number_input("Внутренний радиус, мм", min_value=0.0, max_value=max(0.01, outer_r-0.01), value=min(25.0, outer_r*0.6), step=1.0)
            st.caption("Кольцо — наружный контур минус внутренний. Если стенка тонкая, уменьшайте шаг дорожек и edge_offset.")
        elif shape_ru == "Эллипс":
            params["radius_x"] = st.number_input("Радиус X, мм", min_value=0.1, max_value=1000.0, value=45.0, step=1.0)
            params["radius_y"] = st.number_input("Радиус Y, мм", min_value=0.1, max_value=1000.0, value=25.0, step=1.0)
            st.caption("Эллипс — полезен для овальных сечений. Больший радиус увеличивает длину дорожек и время.")
        elif shape_ru == "Треугольник":
            params["base"] = st.number_input("Основание X, мм", min_value=0.1, max_value=2000.0, value=60.0, step=1.0)
            params["triangle_height"] = st.number_input("Высота треугольника Y, мм", min_value=0.1, max_value=2000.0, value=50.0, step=1.0)
            st.caption("В острых углах траектории короткие. Уменьшайте min_segment_length и включайте контур осторожно.")
        elif shape_ru == "Многоугольник":
            params["sides"] = st.number_input("Количество сторон", min_value=3, max_value=64, value=6, step=1)
            params["radius"] = st.number_input("Радиус описанной окружности, мм", min_value=0.1, max_value=1000.0, value=35.0, step=1.0)
            params["rotation_deg"] = st.number_input("Поворот, град", min_value=-360.0, max_value=360.0, value=0.0, step=5.0)
            st.caption("Больше сторон — ближе к кругу; меньше сторон — больше углов и локальных перегревов на вершинах.")
        elif shape_ru == "Звезда":
            params["points"] = st.number_input("Лучей", min_value=3, max_value=32, value=5, step=1)
            outer_r = st.number_input("Внешний радиус, мм", min_value=0.1, max_value=1000.0, value=40.0, step=1.0)
            params["outer_radius"] = outer_r
            params["inner_radius"] = st.number_input("Внутренний радиус, мм", min_value=0.1, max_value=max(0.1, outer_r*0.95), value=min(20.0, outer_r*0.5), step=1.0)
            params["rotation_deg"] = st.number_input("Поворот, град", min_value=-360.0, max_value=360.0, value=-90.0, step=5.0)
            st.caption("Звезда сложная для наплавки: острые углы любят перегреваться и деформироваться.")
        else:
            params["width_x"] = st.number_input("Ширина X, мм", min_value=0.1, max_value=1000.0, value=25.0, step=1.0)
            params["length_y"] = st.number_input("Длина Y, мм", min_value=0.1, max_value=2000.0, value=100.0, step=1.0)
            st.caption("Капсула — прямой участок с полукруглыми торцами. Хороший вариант без острых углов.")
    rot_xy, mirror_2d_x, mirror_2d_y = _sidebar_2d_placement_controls(simple_mode=not advanced_mode)
    try:
        polys = _cached_standard_shape(shape_map[shape_ru], tuple(sorted(params.items())))
        polys = _transform_polygons_2d(polys, rot_xy, mirror_2d_x, mirror_2d_y)
        base_summary = polygon_summary(polys, height_2d)
    except Exception as exc:
        st.error(f"Не удалось построить стандартную фигуру: {exc}")
        st.stop()
    settings, time_plan = build_settings(base_summary, mode, material_key, advanced_mode=advanced_mode)
    try:
        from shapely.affinity import translate
        if not settings.center_xy:
            from shapely import unary_union
            u = unary_union(polys)
            minx, miny, _, _ = u.bounds
            polys = [translate(p, xoff=-minx, yoff=-miny) for p in polys]
        summary = polygon_summary(polys, height_2d)
        summary["source_type"] = "standard shape: " + shape_ru
        source_info = "Стандартная фигура: " + shape_ru
    except Exception as exc:
        st.error(f"Ошибка подготовки стандартной фигуры: {exc}")
        st.stop()

elif src_type == "STL 3D":
    uploaded = st.sidebar.file_uploader("Загрузить STL", type=["stl"], help="Загрузите STL-модель для нарезки на слои и генерации EBAM G-code.")
    if uploaded is None:
        st.info("Загрузите STL-файл слева.")
        st.stop()
    with st.sidebar:
        st.header("5. Положение STL")
        if advanced_mode:
            axis_order = st.selectbox("Какие исходные оси станут X/Y/Z", ["XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"], index=0, help="Если деталь лежит на боку, выберите порядок, где высота модели станет выходной осью Z.")
            col_rx, col_ry, col_rz = st.columns(3)
            with col_rx:
                rotate_x = st.number_input("Rx", min_value=-360.0, max_value=360.0, value=0.0, step=90.0)
            with col_ry:
                rotate_y = st.number_input("Ry", min_value=-360.0, max_value=360.0, value=0.0, step=90.0)
            with col_rz:
                rotate_z = st.number_input("Rz", min_value=-360.0, max_value=360.0, value=0.0, step=90.0)
            mirror_x = st.checkbox("Зеркально STL по X", value=False)
            mirror_y = st.checkbox("Зеркально STL по Y", value=False)
            mirror_z = st.checkbox("Зеркально STL по Z", value=False)
            st.caption("После ориентации модель автоматически ставится нижней точкой на Z=0. Центрирование XY задаётся ниже.")
        else:
            preset = st.selectbox(
                "Быстрый вариант",
                ["Как в STL", "Высота была по X", "Высота была по Y"],
                index=0,
                help="Если после загрузки высота/габариты выглядят неправильно, выберите другой вариант и посмотрите предпросмотр."
            )
            axis_order = "XYZ"
            if preset == "Высота была по X":
                axis_order = "YZX"
            elif preset == "Высота была по Y":
                axis_order = "XZY"
            rotate_x, rotate_y, rotate_z = 0.0, 0.0, 0.0
            st.caption("Ниже можно вручную поменять направления по осям.")
            col_fx, col_fy, col_fz = st.columns(3)
            with col_fx:
                mirror_x = st.checkbox("-X", value=False)
            with col_fy:
                mirror_y = st.checkbox("-Y", value=False)
            with col_fz:
                mirror_z = st.checkbox("-Z", value=False)
            rotate_z = st.selectbox("Поворот на столе вокруг Z", [0, 90, 180, 270], index=0)
            st.caption("Модель автоматически ставится нижней точкой на Z=0.")
    try:
        raw_mesh = _cached_mesh_from_bytes(uploaded.getvalue(), ".stl")
        orient_settings = ProcessSettings(axis_order=axis_order, rotate_x_deg=rotate_x, rotate_y_deg=rotate_y, rotate_z_deg=rotate_z, mirror_x=mirror_x, mirror_y=mirror_y, mirror_z=mirror_z)
        mesh_for_summary = normalize_mesh(raw_mesh, orient_settings)
        base_summary = mesh_summary(mesh_for_summary)
    except Exception as exc:
        st.error(f"Не удалось прочитать/ориентировать STL: {exc}")
        st.stop()
    settings, time_plan = build_settings(base_summary, mode, material_key, advanced_mode=advanced_mode)
    settings.axis_order = axis_order
    settings.rotate_x_deg = rotate_x
    settings.rotate_y_deg = rotate_y
    settings.rotate_z_deg = rotate_z
    settings.mirror_x = mirror_x
    settings.mirror_y = mirror_y
    settings.mirror_z = mirror_z
    try:
        mesh = normalize_mesh(raw_mesh, settings)
        summary = mesh_summary(mesh)
        source_info = "STL"
    except Exception as exc:
        st.error(f"Ошибка подготовки STL: {exc}")
        st.stop()

elif src_type == "DXF 2D + высота":
    uploaded = st.sidebar.file_uploader("Загрузить DXF", type=["dxf"], help="Загрузите 2D DXF с замкнутым контуром. Приложение вытянет его на заданную высоту Z.")
    height_2d = st.sidebar.number_input("Высота построения, мм", min_value=0.1, max_value=1000.0, value=100.0, step=1.0, help="Высота 3D-построения для загруженного 2D-контура DXF/CSV.")
    rot_xy, mirror_2d_x, mirror_2d_y = _sidebar_2d_placement_controls(simple_mode=not advanced_mode)
    if uploaded is None:
        st.info("Загрузите 2D DXF с замкнутым контуром слева.")
        st.stop()
    try:
        polys = _cached_dxf_polygons_from_bytes(uploaded.getvalue())
        polys = _transform_polygons_2d(polys, rot_xy, mirror_2d_x, mirror_2d_y)
        base_summary = polygon_summary(polys, height_2d)
    except Exception as exc:
        st.error(f"Не удалось прочитать DXF: {exc}")
        st.stop()
    settings, time_plan = build_settings(base_summary, mode, material_key, advanced_mode=advanced_mode)
    # normalize is done inside generate_from_polygons; for preview shift here similarly not necessary if user selected center later
    try:
        from shapely.affinity import translate
        if not settings.center_xy:
            from shapely import unary_union
            u = unary_union(polys)
            minx, miny, _, _ = u.bounds
            polys = [translate(p, xoff=-minx, yoff=-miny) for p in polys]
        summary = polygon_summary(polys, height_2d)
        source_info = "DXF"
    except Exception as exc:
        st.error(f"Ошибка подготовки DXF: {exc}")
        st.stop()

else:
    uploaded = st.sidebar.file_uploader("Загрузить CSV", type=["csv", "txt"], help="Загрузите CSV/TXT с точками X,Y замкнутого контура. Не используйте файл *_layers.csv.")
    height_2d = st.sidebar.number_input("Высота построения, мм", min_value=0.1, max_value=1000.0, value=100.0, step=1.0, help="Высота 3D-построения для загруженного 2D-контура DXF/CSV.")
    rot_xy, mirror_2d_x, mirror_2d_y = _sidebar_2d_placement_controls(simple_mode=not advanced_mode)
    st.sidebar.caption("CSV-геометрия: колонки X,Y или первые две числовые колонки. Не загружайте *_layers.csv — это отчёт по слоям, не контур детали.")
    if uploaded is None:
        st.info("Загрузите контурный CSV с точками X,Y слева. Файл *_layers.csv после генерации G-code сюда загружать нельзя — он нужен только для анализа слоёв.")
        st.stop()
    text = uploaded.getvalue().decode("utf-8-sig", errors="replace")
    try:
        polys = _cached_csv_polygons_from_text(text)
        polys = _transform_polygons_2d(polys, rot_xy, mirror_2d_x, mirror_2d_y)
        base_summary = polygon_summary(polys, height_2d)
    except Exception as exc:
        st.error(f"Не удалось прочитать CSV как контур X,Y: {exc}")
        st.info("Если вы загрузили файл *_layers.csv, это отчёт по слоям после генерации. Он не содержит контура детали. Для новой генерации используйте исходный STL/DXF/CSV X,Y или settings.json во вкладке JSON.")
        st.stop()
    settings, time_plan = build_settings(base_summary, mode, material_key, advanced_mode=advanced_mode)
    try:
        from shapely.affinity import translate
        if not settings.center_xy:
            from shapely import unary_union
            u = unary_union(polys)
            minx, miny, _, _ = u.bounds
            polys = [translate(p, xoff=-minx, yoff=-miny) for p in polys]
        summary = polygon_summary(polys, height_2d)
        source_info = "CSV"
    except Exception as exc:
        st.error(f"Ошибка подготовки CSV: {exc}")
        st.stop()


# ------------------------- optional experience profile from calibration tab -------------------------
_experience_json_from_session = st.session_state.get("experience_profile_json", "")
if st.session_state.get("experience_profile_apply_now", False) and _experience_json_from_session:
    settings = replace(
        settings,
        experience_profile_enabled=True,
        experience_profile_json=str(_experience_json_from_session),
        experience_profile_apply_cfeed=bool(st.session_state.get("experience_profile_apply_cfeed", True)),
        experience_profile_apply_wire=bool(st.session_state.get("experience_profile_apply_wire", True)),
        experience_profile_apply_current=bool(st.session_state.get("experience_profile_apply_current", False)),
        experience_profile_apply_z_step=bool(st.session_state.get("experience_profile_apply_z_step", True)),
        experience_profile_update_m68_at_zone_boundaries=bool(st.session_state.get("experience_profile_update_m68_at_zone_boundaries", False)),
    )

# ------------------------- main tabs -------------------------
tabs = st.tabs(["🏠 Сводка", "📘 Инструкция", "👁️ Предпросмотр", "⚙️ Генерация", "🧩 JSON", "🧪 Калибровка", "📋 Профили и журнал"])

with tabs[0]:
    st.caption(f"Режим интерфейса: {ui_mode_ru}. Технологическая цель: {mode}. Материал: {mat_labels[material_key]}.")
    _focus = bool(time_plan.get("enabled"))
    if _focus:
        st.info("🎯 Режим целевого времени: ниже показан только итоговый режим. Полные таблицы и рекомендации свёрнуты — раскрой при необходимости.")
    col_a, col_b = (st.columns([1, 3]) if _focus else st.columns([1, 1]))
    with col_a:
        _geo_box = st.expander("Анализ геометрии", expanded=False) if _focus else st.container()
        with _geo_box:
            st.subheader(f"Анализ геометрии: {source_info}")
            summary_df = pd.DataFrame({"значение": [str(v) for v in summary.values()]}, index=list(summary.keys()))
            st.table(summary_df)
            if src_type == "STL 3D" and not summary["is_watertight"]:
                st.warning("STL не замкнутый. Сечения могут получиться неполными. Для точного расчёта лучше замкнутая STL-модель.")
    with col_b:
        _calc_box = st.expander("📊 Все расчётные значения (полная таблица)", expanded=False) if _focus else st.container()
        if not _focus:
            st.subheader("Расчётные значения")
        height = max(float(summary["size_z"]), 1e-9)
        n_layers = int(math.ceil(height / settings.layer_height))
        area_wire = settings.wire_area_mm2()
        cinfo = time_plan.get("c_limit_info")
        recalc = _process_recalc_snapshot(settings, cinfo)
        ce = _display_current_energy_pair(settings)
        current_bottom_need = recalc["effective_current_bottom_need"]
        current_top_need = recalc["effective_current_top_need"]
        current_bottom = recalc["effective_current_bottom"]
        current_top = recalc["effective_current_top"]
        wire_bottom = recalc["effective_wire_bottom"]
        wire_top = recalc["effective_wire_top"]
        actual_j_bottom = recalc["effective_actual_energy_bottom"]
        actual_j_top = recalc["effective_actual_energy_top"]
        target_j_bottom = recalc["effective_energy_bottom"]
        target_j_top = recalc["effective_energy_top"]
        evol_target_bottom = target_j_bottom / max(settings.layer_height * settings.hatch_spacing, 1e-9)
        evol_target_top = target_j_top / max(settings.layer_height * settings.hatch_spacing, 1e-9)
        evol_actual_bottom = actual_j_bottom / max(settings.layer_height * settings.hatch_spacing, 1e-9)
        evol_actual_top = actual_j_top / max(settings.layer_height * settings.hatch_spacing, 1e-9)
        eta = _estimate_time_before_generation(summary, settings)
        from ebam_gcode_studio.core import _effective_rotational_radial_step as _err_step
        calc_rows = _build_calc_rows(settings, n_layers, area_wire, recalc, cinfo, eta, _fmt_hm, _err_step)
        calc_df = pd.DataFrame(calc_rows, columns=["Параметр", "Значение"])
        calc_df["Значение"] = calc_df["Значение"].map(str)
        with _calc_box:
            st.table(calc_df.set_index("Параметр"))
        cinfo = time_plan.get("c_limit_info")
        if cinfo and cinfo.get("radius_below_warning"):
            st.warning(
                f"Реальный минимальный C-радиус {cinfo['real_radius_mm']:.2f} мм ниже порога предупреждения "
                f"{cinfo['warning_radius_mm']:.2f} мм. Это предупреждение о малом радиусе; F ограничивается по реальному радиусу, а не по порогу."
            )
        if cinfo:
            if cinfo.get("limited"):
                st.warning(
                    "Скорость F ограничена C-осью: задано "
                    f"{cinfo['requested_bottom']:.1f}/{cinfo['requested_top']:.1f} мм/мин, "
                    f"фактически используется {cinfo['effective_bottom']:.1f}/{cinfo['effective_top']:.1f} мм/мин. "
                    f"Требовалось C {cinfo['required_c_bottom']:.1f}/{cinfo['required_c_top']:.1f} град/мин, "
                    f"разрешено {cinfo['cmax_deg_min']:.1f} град/мин. "
                    f"Радиус для C-лимита: {cinfo['radius_mm']:.2f} мм, источник: {cinfo.get('radius_source','—')}. "
                    "E0/E2/энергия/время считаются по фактической F."
                )
                st.table(pd.DataFrame({
                    "Параметр": ["F", "E2", "E0 нужен/задан", "E0 в G-code", "Энергия факт"],
                    "До C-ограничителя": [
                        f"{recalc['requested_feed_bottom']:.1f}/{recalc['requested_feed_top']:.1f} мм/мин",
                        f"{recalc['requested_wire_bottom']:.3f}/{recalc['requested_wire_top']:.3f} мм/с",
                        f"{recalc['requested_current_bottom_need']:.3f}/{recalc['requested_current_top_need']:.3f} мА",
                        "—",
                        "—",
                    ],
                    "После C-ограничителя": [
                        f"{recalc['effective_feed_bottom']:.1f}/{recalc['effective_feed_top']:.1f} мм/мин",
                        f"{recalc['effective_wire_bottom']:.3f}/{recalc['effective_wire_top']:.3f} мм/с",
                        f"{recalc['effective_current_bottom_need']:.3f}/{recalc['effective_current_top_need']:.3f} мА",
                        f"{recalc['effective_current_bottom']:.3f}/{recalc['effective_current_top']:.3f} мА",
                        f"{recalc['effective_actual_energy_bottom']:.1f}/{recalc['effective_actual_energy_top']:.1f} Дж/мм",
                    ],
                }).set_index("Параметр"))
            elif not _focus:
                st.info(
                    f"C-ограничитель: {cinfo['reason']}. Требуется C "
                    f"{cinfo['required_c_bottom']:.1f}/{cinfo['required_c_top']:.1f} град/мин, "
                    f"разрешено {cinfo['cmax_deg_min']:.1f} град/мин. "
                    f"Радиус для C-лимита: {cinfo['radius_mm']:.2f} мм, источник: {cinfo.get('radius_source','—')}."
                )
        if eta.get("strategy_note") and not _focus:
            st.caption("Оценка времени учитывает стратегию: " + str(eta.get("strategy_note")))

        gate_level, gate_title, gate_messages = _safety_gate_status(
            src_type, summary, settings, time_plan, n_layers, wire_bottom, wire_top, current_bottom_need, current_top_need
        )
        gate_text = "\n".join(f"- {m}" for m in gate_messages)
        if gate_level == "bad":
            st.error(f"{gate_title}\n\n{gate_text}")
        elif gate_level == "warn":
            st.warning(f"{gate_title}\n\n{gate_text}")
        elif _focus:
            st.caption("✅ " + gate_title)
        else:
            st.success(f"{gate_title}\n\n{gate_text}")

        _acc_t = float(st.session_state.get("_accepted_target_s") or 0.0)
        if _acc_t > 0 and not time_plan.get("enabled"):
            _cur_t = float(eta.get("total_s", 0.0))
            _dlt = 100.0 * (_cur_t - _acc_t) / max(_acc_t, 1.0)
            _dc1, _dc2 = st.columns([5, 1])
            with _dc1:
                st.info(f"🎯 База принята от цели {_fmt_hm(_acc_t)} → с текущими параметрами получится {_fmt_hm(_cur_t)} ({_dlt:+.1f}%). Корректируй поля слева.")
            with _dc2:
                st.button("✖ Сброс", key="btn_clear_acc", on_click=lambda: st.session_state.pop("_accepted_target_s", None))
        if time_plan.get("enabled"):
            st.markdown("### Подгонка под заданное время")
            target_text = _fmt_hm(time_plan.get("target_s", 0))
            base_text = _fmt_hm(time_plan.get("base_total_s", 0))
            adj_text = _fmt_hm(time_plan.get("adjusted_total_s", 0))
            delta = time_plan.get("error_pct", 0.0)
            if time_plan.get("severity") == "ok":
                st.success(f"Цель: {target_text}. Было примерно {base_text}, после подстройки примерно {adj_text} ({delta:+.1f}%).")
            elif time_plan.get("severity") == "warn":
                st.warning(f"Цель: {target_text}. Было примерно {base_text}, после подстройки примерно {adj_text} ({delta:+.1f}%). Нужна проверка на TEST-файле.")
            else:
                st.error(f"Цель: {target_text}. Было примерно {base_text}, после подстройки примерно {adj_text} ({delta:+.1f}%). Вероятно, в это время логично не уложиться без ухудшения режима или выхода за ограничения.")
            applied = time_plan.get("applied_settings", {})
            if applied:
                applied_df = pd.DataFrame({
                    "Параметр": ["Z-шаг, мм", "Шаг дорожек, мм", "Скорость низ, мм/мин", "Скорость верх, мм/мин", "Энергия низ, Дж/мм", "Энергия верх, Дж/мм", "Пауза низ/верх, с", "Лимит тока E0, мА"],
                    "Применено": [
                        round(applied.get("layer_height", settings.layer_height), 3),
                        round(applied.get("hatch_spacing", settings.hatch_spacing), 3),
                        round(applied.get("feed_bottom", settings.feed_bottom_mm_min), 1),
                        round(applied.get("feed_top", settings.feed_top_mm_min), 1),
                        round(applied.get("energy_bottom", settings.target_energy_bottom_j_per_mm), 1),
                        round(applied.get("energy_top", settings.target_energy_top_j_per_mm), 1),
                        f"{applied.get('pause_bottom', settings.layer_pause_bottom_s):.2f} / {applied.get('pause_top', settings.layer_pause_top_s):.2f}",
                        round(applied.get("current_max", settings.current_max_ma), 1),
                    ]
                })
                applied_df["Применено"] = applied_df["Применено"].map(str)
                st.table(applied_df.set_index("Параметр"))
            _mode_rows = [
                ("Радиальный шаг колец, мм", f"{_err_step(settings):.3f}"),
                ("Подача проволоки E2 низ/верх, мм/с", f"{wire_bottom:.2f} / {wire_top:.2f}  (граница {settings.wire_max_mm_s:.1f})"),
                ("Ток в G-code низ/верх, мА", f"{current_bottom:.2f} / {current_top:.2f}  (лимит {settings.current_max_ma:.1f})"),
                ("Мощность пучка низ/верх, Вт", f"{settings.voltage_kv*current_bottom:.0f} / {settings.voltage_kv*current_top:.0f}  (порог {settings.min_beam_power_w:.0f})"),
            ]
            _ci = time_plan.get("c_limit_info") or {}
            if _ci.get("required_c_bottom") is not None:
                _mode_rows.append(("C требуется низ/верх, град/мин",
                                   f"{_ci.get('required_c_bottom', 0):.0f} / {_ci.get('required_c_top', 0):.0f}  (разрешено {_ci.get('cmax_deg_min', 0):.0f})"))
            _mode_rows += [
                ("Время активной наплавки", _fmt_hm(eta.get("active_s", 0.0))),
                ("Паузы и тепловые выдержки", _fmt_hm(max(0.0, eta.get("total_s", 0.0) - eta.get("active_s", 0.0)))),
                ("ПОЛНОЕ время", _fmt_hm(eta.get("total_s", 0.0))),
            ]
            st.markdown("**Итог режима под целевое время**")
            st.table(pd.DataFrame(_mode_rows, columns=["Параметр", "Значение"]).set_index("Параметр"))
            _ap = time_plan.get("applied_settings", {}) or {}
            _hv = float(_ap.get("hatch_spacing", settings.hatch_spacing))
            _base_payload = {
                "w_adv_layer": float(_ap.get("layer_height", settings.layer_height)),
                "w_s_layer": float(_ap.get("layer_height", settings.layer_height)),
                "w_adv_hatch": _hv, "w_s_hatch": _hv,
                "w_adv_f0": float(_ap.get("feed_bottom", settings.feed_bottom_mm_min)),
                "w_adv_f1": float(_ap.get("feed_top", settings.feed_top_mm_min)),
                "w_s_f0": float(_ap.get("feed_bottom", settings.feed_bottom_mm_min)),
                "w_s_f1": float(_ap.get("feed_top", settings.feed_top_mm_min)),
                "w_adv_e0": float(_ap.get("energy_bottom", settings.target_energy_bottom_j_per_mm)),
                "w_adv_e1": float(_ap.get("energy_top", settings.target_energy_top_j_per_mm)),
                "w_adv_imax": float(_ap.get("current_max", settings.current_max_ma)),
                "w_s_imax": float(_ap.get("current_max", settings.current_max_ma)),
                "adv_radial_step": float(getattr(settings, "rotational_radial_step_mm", 0.0) or _hv),
                "simple_radial_step": float(getattr(settings, "rotational_radial_step_mm", 0.0) or _hv),
                "w_tt_adv": False, "w_tt_simple": False,
                "_accepted_target_s": float(time_plan.get("target_s", 0.0) or 0.0),
            }
            def _accept_base_cb(_p=_base_payload):
                for _k, _v in _p.items():
                    st.session_state[_k] = _v
            st.button("📌 Принять как базу и корректировать", key="btn_accept_base",
                      on_click=_accept_base_cb,
                      help="Переносит подобранные значения в поля слева и выключает подгонку. Дальше правь любой параметр — сводка покажет новое время и отклонение от принятой цели.")
            _crit_kw = ("выше вашей контрольной границы", "не уложиться", "Минимально достижимо", "разрешите")
            _msgs = list(dict.fromkeys(time_plan.get("messages", [])))
            _crit = [m for m in _msgs if any(k in m for k in _crit_kw)]
            _soft = [m for m in _msgs if m not in _crit]
            for msg in _crit:
                st.warning(msg)
            if _soft:
                _det_box = st.expander("Подробности и рекомендации", expanded=False) if _focus else st.container()
                with _det_box:
                    for msg in _soft:
                        st.warning(msg)
        max_wire_calc = max(wire_bottom, wire_top)
        if max_wire_calc > settings.wire_max_mm_s and not _focus:
            st.warning(
                f"Расчётная подача проволоки {max_wire_calc:.3f} мм/с выше вашей контрольной границы {settings.wire_max_mm_s:.3f} мм/с. "
                "В v4.2 это НЕ ошибка и НЕ автоматический зажим: G-code будет рассчитан с фактической требуемой подачей. "
                "Если механизм подачи реально допускает такую скорость — увеличьте контрольную границу. Если нет — уменьшите Z-шаг, шаг дорожек или скорость F."
            )
        elif max_wire_calc > settings.wire_max_mm_s * 0.85 and not _focus:
            st.info("Подача проволоки близко к верхней контрольной границе. Для первого теста проверьте устойчивость подачи и W-ретракт на сухом прогоне.")
        if current_bottom_need > settings.current_max_ma or current_top_need > settings.current_max_ma:
            st.error(
                f"Для выбранных Дж/мм и F нужен ток до {max(current_bottom_need, current_top_need):.2f} мА, "
                f"но лимит пучка задан {settings.current_max_ma:.2f} мА. "
                "В G-code ток будет ограничен этим лимитом, поэтому фактическая энергия будет ниже расчётной. "
                "Можно поднять лимит тока, уменьшить F или снизить требуемые Дж/мм."
            )
        elif max(current_bottom_need, current_top_need) > settings.current_max_ma * 0.85:
            st.info("Ток пучка близко к заданному лимиту. Для первого теста оставьте запас, особенно на тонких/острых участках.")
        if settings.contour_passes > 0 and not _focus:
            st.warning("Контурные проходы улучшают край, но для боковой подачи проволоки часть контура может идти в неидеальном направлении. Проверять на коротком тесте.")

with tabs[1]:
    st.subheader("Инструкция и подсказки")
    st.markdown(
        """
        ### Быстрый маршрут для новичка
        1. Выбери **Простой** режим.
        2. Загрузи STL или выбери стандартную фигуру.
        3. Выбери материал и цель: **Качество / Баланс / Скорость**.
        4. Открой **Предпросмотр** и проверь, что сечение и штриховка похожи на ожидаемую деталь.
        5. Вкладка **Генерация** → сначала включи **TEST-файл** до 10–15 мм.
        6. Скачай `.ngc` и `audit.txt`, проверь в viewer/LinuxCNC, затем делай сухой прогон без луча и проволоки.

        ### Когда нужен расширенный режим
        Расширенный режим нужен, если надо вручную менять Z-шаг, шаг дорожек, энергию Дж/мм, скорость F,
        токовый лимит, W-ретракт, контурные проходы, резервные сечения STL или подгонку под время.
        """
    )
    st.markdown("### Как параметры влияют на форму и качество")
    show_parameter_effects()
    st.markdown(
        "**Практическое правило:** если фигура расплывается и верх блестит — снижать энергию/ток или увеличивать скорость. "
        "Если проволока тыкается и валик рвётся — энергии мало, проволоки много или Z-шаг завышен."
    )
    st.markdown(
        "**По стандартным фигурам:** увеличение размера X/Y/R увеличивает длину активной траектории и расход проволоки; "
        "острые углы у треугольника, звезды и малого многоугольника требуют меньшей скорости/меньшего контурного усиления; "
        "тонкие кольца требуют меньшего `edge_offset`, меньшего `hatch_spacing` и меньшего `min_segment_length`."
    )

with tabs[2]:
    st.subheader("Предпросмотр")
    col_p1, col_p2 = st.columns([1, 1])
    with col_p1:
        st.markdown("### 3D-модель")
        if src_type == "STL 3D":
            try:
                fig = plt.figure(figsize=(7, 5))
                ax = fig.add_subplot(111, projection="3d")
                verts = mesh.vertices
                faces = mesh.faces
                step = max(1, len(faces)//5000)
                tri = verts[faces[::step]]
                from mpl_toolkits.mplot3d.art3d import Poly3DCollection
                pc = Poly3DCollection(tri, alpha=0.25)
                ax.add_collection3d(pc)
                ax.auto_scale_xyz(verts[:,0], verts[:,1], verts[:,2])
                try:
                    dx = max(float(summary.get("size_x", 1.0)), 1.0)
                    dy = max(float(summary.get("size_y", 1.0)), 1.0)
                    dz = max(float(summary.get("size_z", 1.0)), 1.0)
                    ax.set_box_aspect((dx, dy, dz))
                except Exception:
                    pass
                ax.set_xlabel("X, мм")
                ax.set_ylabel("Y, мм")
                ax.set_zlabel("Z, мм")
                st.pyplot(fig)
                plt.close(fig)
            except Exception as exc:
                st.warning(f"3D-предпросмотр недоступен: {exc}")
        elif src_type == "Чаши/баллоны":
            try:
                draw_rotational_vessel_3d(rotational_params)
                st.caption("Показана номинальная 3D-геометрия детали: наружная поверхность, внутренняя полость и донце. Это именно форма детали, а не только сечение слоя.")
                with st.expander("Профиль по высоте / радиусу", expanded=False):
                    draw_rotational_vessel_profile(rotational_params)
            except Exception as exc:
                st.warning(f"3D-модель чаши/баллона не построена: {exc}")
        else:
            try:
                draw_3d_preview_from_polys(polys, float(height_2d), title=f"3D-модель: {source_info}")
            except Exception as exc:
                st.warning(f"3D-предпросмотр не построен: {exc}")

    with col_p2:
        st.markdown("### Сечение и траектории")
        if src_type == "STL 3D":
            z_fraction = st.slider("Сечение по высоте, доля Z", 0.01, 0.99, 0.50, 0.01)
            zmid = float(summary["size_z"]) * z_fraction
            try:
                probe = settings.layer_height * settings.section_probe_fraction if settings.adaptive_section_probe else 0.0
                sec_polys = _section_polygons_at_z(mesh, zmid, probe_radius=probe)
                used_z = zmid
                if not sec_polys and settings.adaptive_section_probe:
                    best = []
                    best_z = zmid
                    search_span = max(settings.layer_height * 3.0, float(summary["size_z"]) * 0.02)
                    for k in range(1, 21):
                        dz = search_span * k / 20.0
                        for zz in (zmid - dz, zmid + dz):
                            if zz <= 0 or zz >= float(summary["size_z"]):
                                continue
                            cand = _section_polygons_at_z(mesh, zz, probe_radius=probe)
                            if cand and sum(p.area for p in cand) > sum(p.area for p in best):
                                best = cand
                                best_z = zz
                    sec_polys = best
                    used_z = best_z

                layer_index = max(1, int(round(zmid / max(settings.layer_height, 1e-9))) + 1)
                title = f"Сечение Z={zmid:.2f} мм" if abs(used_z - zmid) < 1e-6 else f"Сечение Z={zmid:.2f} мм, найдено рядом Z={used_z:.2f} мм"
                preview_mode = str(getattr(settings, "rotational_path_strategy", "hatch")).lower()
                if preview_mode in ("stl_rotary_c_rings", "mesh_rotary_c_rings", "rotary_c", "rotary_c_rings", "c_rings", "c_table", "rings", "xy_rings", "spiral", "xy_spiral"):
                    draw_generic_special_path_preview(sec_polys, settings, layer_index, title=f"{_rotational_strategy_ru(preview_mode)} STL, {title}")
                else:
                    draw_preview_from_polys(sec_polys, settings, layer_index, title=title)
            except Exception as exc:
                st.warning(f"Предпросмотр не построен: {exc}")
        elif src_type == "Чаши/баллоны":
            try:
                z_fraction = st.slider("Сечение чаши/баллона по высоте, доля Z", 0.0, 1.0, 0.50, 0.01)
                zshow = float(summary["size_z"]) * z_fraction
                sec_polys = rotational_shell_polygons_at_z(zshow, rotational_params)
                layer_index = max(1, int(round(zshow / max(settings.layer_height, 1e-9))) + 1)
                rmode = str(getattr(settings, "rotational_path_strategy", "hatch")).lower()
                if rmode in ("rotary_c", "rotary_c_rings", "c_rings", "c_table"):
                    rsegs = rotational_ring_segments_at_z(zshow, rotational_params, settings, layer_index)
                    draw_rotational_path_preview(sec_polys, rsegs, settings, title=f"Поворотный стол C {source_info}, Z={zshow:.2f} мм")
                    radii = rotational_layer_radii_at_z(zshow, rotational_params, settings)
                    if radii:
                        req = [rotary_c_speed_deg_min(settings.feed_bottom_mm_min, max(r, 1e-9)) for r in radii]
                        st.caption(f"Предпросмотр C-режима: визуально показаны кольца, но в G-code они будут выполняться как X=центр+R и G91 C360. Радиусы {min(radii):.2f}…{max(radii):.2f} мм; оценка скорости C при Fниз: {min(req):.0f}…{max(req):.0f} град/мин.")
                    else:
                        st.caption("Предпросмотр C-режима: на этой высоте нет радиусов для C-колец.")
                elif rmode == "rings":
                    rsegs = rotational_ring_segments_at_z(zshow, rotational_params, settings, layer_index)
                    draw_rotational_path_preview(sec_polys, rsegs, settings, title=f"Кольца {source_info}, Z={zshow:.2f} мм")
                    st.caption("Это специализированная XY-круговая стратегия: G-code идёт по X/Y-кольцам. C-режим вместо этого вращает стол C.")
                elif rmode == "spiral":
                    rsegs = rotational_spiral_segments_at_z(zshow, rotational_params, settings, layer_index)
                    draw_rotational_path_preview(sec_polys, rsegs, settings, title=f"Спираль {source_info}, Z={zshow:.2f} мм")
                    st.caption("Это специализированная спиральная стратегия внутри слоя: меньше стартов/остановов, но обязательно нужен короткий TEST.")
                else:
                    draw_preview_from_polys(sec_polys, settings, layer_index, title=f"Сечение {source_info}, Z={zshow:.2f} мм")
                    st.caption("Старый режим: кольцевое сечение заполняется обычными дорожками выбранного плана проходов внутри слоя.")
            except Exception as exc:
                st.warning(f"Предпросмотр чаши/баллона не построен: {exc}")
        else:
            try:
                preview_mode = str(getattr(settings, "rotational_path_strategy", "hatch")).lower()
                if preview_mode in ("rotary_c", "rotary_c_rings", "c_rings", "c_table", "generic_rotary_c_rings", "rings", "xy_rings", "spiral", "xy_spiral"):
                    draw_generic_special_path_preview(polys, settings, 1, title=f"{_rotational_strategy_ru(preview_mode)}: {source_info}")
                else:
                    draw_preview_from_polys(polys, settings, 1, title=f"Контур {source_info}")
            except Exception as exc:
                st.warning(f"Предпросмотр не построен: {exc}")

with tabs[3]:
    st.subheader("Генерация")
    st.caption("Сохраняйте аудит вместе с G-code. Он показывает диапазоны, предупреждения и расчётные режимы.")

    total_layers_est = int(math.ceil(float(summary["size_z"]) / max(settings.layer_height, 1e-9)))
    st.info(f"Оценка: будет до {total_layers_est} слоёв. Для сложных STL генерация может занимать 1–5 минут на слабом ПК. В v4.2 сохранён прогресс по слоям, чтобы окно не выглядело зависшим.")

    col_g1, col_g2 = st.columns([1, 1])
    with col_g1:
        test_mode = st.checkbox("Сначала сделать короткий TEST-файл", value=False, help="Генерирует только первые слои до указанной высоты. Полезно для проверки Чаши и тяжёлых STL без долгого ожидания полного файла.")
    with col_g2:
        test_height = st.number_input("Высота TEST, мм", min_value=1.0, max_value=max(1.0, float(summary["size_z"])), value=min(15.0, max(1.0, float(summary["size_z"]))), step=1.0)

    if test_mode:
        test_layers = max(1, int(math.ceil(test_height / max(settings.layer_height, 1e-9))))
        st.warning(f"Будет создан НЕ полный файл, а тест до Z≈{test_layers * settings.layer_height:.1f} мм: {test_layers} слоёв из {total_layers_est}.")
    else:
        test_layers = 0
        if total_layers_est > 250:
            st.warning("Полная STL-генерация может быть долгой и файл будет большим. Не закрывайте вкладку, пока идёт прогресс. Для первой проверки лучше включить TEST-файл до Z10–15 мм.")

    if st.button("Сгенерировать G-code", type="primary"):
        run_settings = replace(settings, max_layers_to_generate=int(test_layers), progress_update_every_layers=max(1, total_layers_est // 200))
        progress_bar = st.progress(0.0)
        status_box = st.empty()

        def _progress(done: int, total: int, stage: str):
            total = max(int(total), 1)
            done = max(0, min(int(done), total))
            progress_bar.progress(done / total)
            status_box.write(f"{stage}: слой {done}/{total}")

        with st.spinner("Слайсинг/генерация G-code... окно может работать несколько минут, это нормально для STL."):
            try:
                if src_type == "STL 3D":
                    result = generate_from_mesh(raw_mesh, run_settings, progress_callback=_progress)
                elif src_type == "Чаши/баллоны":
                    result = generate_rotational_shell(rotational_params, run_settings, progress_callback=_progress)
                else:
                    result = generate_from_polygons_2d(polys, float(height_2d), run_settings, progress_callback=_progress)
            except Exception as exc:
                st.error(f"Ошибка генерации: {exc}")
                if src_type == "CSV X,Y + высота":
                    st.info("Проверьте, что CSV содержит именно замкнутый контур X,Y. Служебный файл *_layers.csv после генерации не является входной геометрией.")
                st.stop()
        progress_bar.progress(1.0)
        status_box.write("Готово")
        st.success("G-code сгенерирован")
        with st.expander("🚦 Проверка G-code (light-светофор)", expanded=True):
            _pc = _gtools.post_generation_check(
                result.gcode,
                e0_cap_ma=float(settings.current_max_ma),
                e2_cap_mm_s=float(settings.wire_max_mm_s),
                qv_floor_j_mm3=55.0,
                c_max_deg_min=float(getattr(settings, "rotary_c_max_deg_min", 600.0)),
                min_beam_power_w=float(settings.min_beam_power_w),
                voltage_kv=float(settings.voltage_kv),
            )
            _emoji = {"ok": "🟢", "warn": "🟡", "bad": "🔴"}
            st.markdown(f"**Итог проверки: {_emoji.get(_pc['status'],'⚪')} " +
                        {"ok": "без замечаний", "warn": "есть предупреждения", "bad": "есть критические замечания"}.get(_pc['status'], "") +
                        f"**  (колец {_pc['rings']}, линков {_pc['links']})")
            for _it in _pc["items"]:
                st.markdown(f"{_emoji.get(_it['status'],'⚪')} **{_it['name']}** — {_it['value']}" +
                            (f"  · {_it['note']}" if _it['note'] else ""))
            _wf = _gtools.wire_freeze_check(result.gcode, dwell_threshold_s=60.0)
            st.markdown(f"{_emoji.get(_wf['status'],'⚪')} **Вмерзание кончика проволоки** — {_wf['message']}")
            if _pc["status"] == "bad" or _wf["status"] == "bad":
                st.warning("Есть критические замечания — проверьте перед наплавкой. Это анализ готового G-code, а не блокировка.")
        if result.stats.get("estimated_total_time_s") is not None:
            st.info(
                "Оценка времени по фактическим траекториям: "
                f"активная наплавка {_fmt_hm(result.stats.get('estimated_active_time_s', 0))}, "
                f"полное технологическое время примерно {_fmt_hm(result.stats.get('estimated_total_time_s', 0))}."
            )
        if result.stats.get("target_total_time_s", 0.0):
            err = result.stats.get("target_time_error_pct", 0.0)
            msg = (
                f"Целевое время: {_fmt_hm(result.stats.get('target_total_time_s', 0))}; "
                f"по фактическим траекториям получилось {_fmt_hm(result.stats.get('estimated_total_time_s', 0))} ({err:+.1f}%)."
            )
            if abs(err) <= 15.0:
                st.success(msg)
            else:
                st.warning(msg + " После построения реальных траекторий цель не совпала точно; скорректируйте время или параметры и перегенерируйте.")
        if result.stats.get("wire_above_control_limit"):
            st.warning(
                f"В G-code есть подача проволоки до {result.stats.get('wire_max_calculated_mm_s', 0):.3f} мм/с, "
                f"что выше контрольной границы {settings.wire_max_mm_s:.3f} мм/с. Значение не обрезано автоматически."
            )
        if result.stats.get("current_clipped_by_min"):
            st.warning(
                f"Расчётный ток местами ниже нижнего лимита {settings.current_min_ma:.3f} мА. "
                "В G-code ток поднят до этого лимита, поэтому фактическая энергия Дж/мм выше расчётной. "
                "Если поднимать ток не нужно — поставьте нижний лимит E0 = 0 мА."
            )
        elif result.stats.get("current_below_low_warning"):
            st.warning(
                f"Расчётный ток местами ниже порога предупреждения {settings.current_low_warning_ma:.3f} мА. "
                "G-code не меняется, но проверьте устойчивость реального источника на таком малом токе."
            )

        if result.stats.get("beam_power_below_floor"):
            st.error(
                f"⚠️ МАЛАЯ МОЩНОСТЬ ПРОПЛАВА. Слабейший слой: "
                f"{result.stats.get('beam_power_current_now_ma', 0):.2f} мА → "
                f"{result.stats.get('beam_power_min_w', 0):.0f} Вт при пороге "
                f"{result.stats.get('min_beam_power_w_floor', 0):.0f} Вт. Риск непроплава/шариков "
                f"(реальный случай: 160 Дж/мм + медленное C ≈ 630 Вт). Чтобы выйти на порог при текущей скорости, "
                f"поднимите энергию до ~{result.stats.get('beam_power_energy_needed_j_mm', 0):.0f} Дж/мм "
                f"(~{result.stats.get('beam_power_current_needed_ma', 0):.1f} мА) либо скорость до "
                f"~{result.stats.get('beam_power_speed_for_floor_mm_s', 0):.2f} мм/с. G-code НЕ изменён; "
                f"порог калибруется одиночным валиком (задача 'Калибровка валиков')."
            )
        elif result.stats.get("min_beam_power_w_floor", 0) > 0:
            st.caption(
                f"Мощность пучка: {result.stats.get('beam_power_min_w', 0):.0f}–{result.stats.get('beam_power_max_w', 0):.0f} Вт "
                f"(порог проплава {result.stats.get('min_beam_power_w_floor', 0):.0f} Вт) — в норме."
            )

        suffix = "TEST" if test_mode else "FULL"
        result_zip = _make_result_zip(result, settings_to_json(run_settings), suffix)
        st.download_button(
            "Скачать весь комплект .zip",
            result_zip,
            file_name=f"ebam_result_{APP_FILE_TAG}_{suffix}.zip",
            mime="application/zip",
            type="primary",
        )
        st.download_button("Скачать G-code .ngc", result.gcode, file_name=f"ebam_generated_{APP_FILE_TAG}_{suffix}.ngc", mime="text/plain")
        st.download_button("Скачать таблицу слоёв .csv", result.layer_csv, file_name=f"ebam_layers_{APP_FILE_TAG}_{suffix}.csv", mime="text/csv")
        st.download_button("Скачать аудит .txt", result.audit_text, file_name=f"ebam_audit_{APP_FILE_TAG}_{suffix}.txt", mime="text/plain")
        st.download_button("Скачать настройки .json", settings_to_json(run_settings), file_name=f"ebam_settings_{APP_FILE_TAG}_{suffix}.json", mime="application/json")
        st.subheader("Сводка")
        st.json(result.stats)
        if result.stats.get("gcode_size_mb", 0) > settings.max_gcode_size_mb_warning:
            st.warning("G-code получился большим. Для LinuxCNC лучше сначала открыть в просмотрщике/симуляторе и проверить загрузку программы.")
        st.subheader("Первые строки G-code")
        st.code("\n".join(result.gcode.splitlines()[:100]), language="gcode")
        st.subheader("Аудит")
        st.text(result.audit_text[:16000])

with tabs[4]:
    st.subheader("Параметры JSON")
    st.caption("Можно сохранить профиль режима и потом восстановить вручную/через CLI.")
    settings_json_now = settings_to_json(settings)
    st.download_button("Скачать текущие настройки .json", settings_json_now, file_name=f"ebam_settings_{APP_FILE_TAG}_current.json", mime="application/json")
    st.code(settings_json_now, language="json")


with tabs[5]:
    st.subheader("🧪 Мастер калибровки по одному валику (TEST → параметры)")
    st.caption("Наплавь один валик, замерь ширину и высоту штангелем — мастер посчитает шаг дорожек, "
               "высоту слоя и перекрытие. Это замыкает TEST обратно в параметры (пункт 1).")
    _wc1, _wc2, _wc3 = st.columns(3)
    with _wc1:
        _bw = st.number_input("Ширина валика, мм", min_value=0.0, max_value=30.0, value=0.0, step=0.1, key="wiz_bw")
    with _wc2:
        _bh = st.number_input("Высота валика, мм", min_value=0.0, max_value=15.0, value=0.0, step=0.1, key="wiz_bh")
    with _wc3:
        _om = st.selectbox("Модель перекрытия", ["TOM 0.738·w (Ding)", "FOM 0.667·w (Suryakumar)"], index=0, key="wiz_om")
    _lhf = st.slider("Доля высоты валика на слой (для сплавления)", min_value=0.5, max_value=1.0, value=0.9, step=0.05, key="wiz_lhf")
    if _bw > 0 and _bh > 0:
        _cal = _gtools.calibrate_from_bead(_bw, _bh, "tom" if _om.startswith("TOM") else "fom", float(_lhf))
        _m1, _m2, _m3 = st.columns(3)
        _m1.metric("Шаг дорожек", f"{_cal['hatch_spacing_mm']:.3f} мм")
        _m2.metric("Высота слоя", f"{_cal['layer_height_mm']:.3f} мм")
        _m3.metric("Перекрытие", f"{_cal['overlap_percent']:.0f}%")
        st.caption(f"Валиков на 100 мм ширины: {_cal['beads_per_100mm']:.1f} · соотношение ширина/высота: {_cal['aspect_w_over_h']:.2f}")
        for _n in _cal["notes"]:
            st.warning(_n)
        st.info("Перенеси эти значения в поля «Шаг дорожек» и «Шаг слоя Z» слева (или сохрани профиль во вкладке «Профили и журнал»).")
    else:
        st.caption("Введите ширину и высоту валика, чтобы увидеть рекомендованные шаг и высоту слоя.")
    st.divider()
    st.subheader("Калибровка по фактическому опыту")
    st.caption("Идея из внешнего отчёта: измерили реальную деталь и ручные override - получили следующий зонный профиль C/E2/E0/Z, не копируя высокую E2 слепо.")
    if st.session_state.get("experience_profile_apply_now", False):
        st.success("Профиль калибровки сейчас применён к генератору. Он особенно полезен для режима: Поворотный стол C -> одно кольцо, C без остановки, переход C+Z.")
    else:
        st.info("Профиль пока не применён. Можно рассчитать, скачать JSON/CSV, затем нажать 'Применить профиль к генератору'.")

    # Defaults from current geometry/settings.
    _h = float(summary.get("size_z", 100.0))
    _r_default = 39.5
    try:
        if src_type == "Чаши/баллоны":
            _rs = rotational_layer_radii_at_z(min(_h * 0.5, _h - 1e-4), rotational_params, settings)
            if _rs:
                _r_default = float(sum(_rs) / len(_rs))
    except Exception:
        pass
    _c_default = rotary_c_speed_deg_min(float(settings.feed_bottom_mm_min), _r_default)
    _area = settings.wire_area_mm2()
    _e2_auto_default = max(0.0, float(settings.layer_height) * (float(settings.feed_bottom_mm_min) / 60.0) * float(settings.hatch_spacing) / max(_area * float(settings.deposition_efficiency), 1e-9))
    _e0_default = float(getattr(settings, "beam_current_bottom_ma", 0.0) or 0.0)
    if str(getattr(settings, "beam_current_mode", "energy")).lower() == "energy":
        _e0_default = float(settings.target_energy_bottom_j_per_mm) * float(settings.feed_bottom_mm_min) / max(60.0 * float(settings.voltage_kv), 1e-9)

    colc1, colc2, colc3 = st.columns(3)
    with colc1:
        cal_height = st.number_input("Высота программы, мм", min_value=1.0, max_value=1000.0, value=float(_h), step=1.0, key="cal_height")
        cal_z = st.number_input("Базовый Z-шаг, мм", min_value=0.05, max_value=5.0, value=float(settings.layer_height), step=0.05, format="%.3f", key="cal_z")
        cal_r = st.number_input("Радиус траектории C, мм", min_value=0.1, max_value=1000.0, value=float(_r_default), step=0.5, format="%.3f", key="cal_r")
        cal_c_code = st.number_input("Cfeed исходный, град/мин", min_value=0.0, max_value=20000.0, value=float(_c_default), step=10.0, format="%.3f", key="cal_c_code")
    with colc2:
        cal_e2_code = st.number_input("E2 исходная, мм/с", min_value=0.0, max_value=100.0, value=float(_e2_auto_default), step=0.1, format="%.3f", key="cal_e2_code")
        cal_e0_code = st.number_input("E0 исходный, мА", min_value=0.0, max_value=100.0, value=float(_e0_default), step=0.5, format="%.3f", key="cal_e0_code")
        cal_u = st.number_input("Напряжение U, кВ", min_value=1.0, max_value=200.0, value=float(settings.voltage_kv), step=1.0, format="%.3f", key="cal_u")
        cal_wire_d = st.number_input("Диаметр проволоки, мм", min_value=0.1, max_value=5.0, value=float(settings.wire_diameter_mm), step=0.1, format="%.3f", key="cal_wire_d")
        cal_eta = st.number_input("Коэффициент осаждения η", min_value=0.10, max_value=1.00, value=float(settings.deposition_efficiency), step=0.01, format="%.3f", key="cal_eta")
    with colc3:
        cal_actual_h = st.number_input("Фактическая высота детали, мм", min_value=0.0, max_value=1000.0, value=min(float(_h), 88.25), step=0.5, format="%.3f", key="cal_actual_h")
        cal_od = st.number_input("Наружный диаметр OD, мм", min_value=0.0, max_value=1000.0, value=86.13, step=0.5, format="%.3f", key="cal_od")
        cal_id = st.number_input("Внутренний диаметр ID, мм", min_value=0.0, max_value=1000.0, value=71.82, step=0.5, format="%.3f", key="cal_id")
        cal_wall_meas = st.number_input("Измеренная стенка, мм (0 = по OD/ID)", min_value=0.0, max_value=200.0, value=0.0, step=0.1, format="%.3f", key="cal_wall_meas")

    st.markdown("### Цель и ручные поправки")
    colg1, colg2, colg3 = st.columns(3)
    with colg1:
        cal_target_wall = st.number_input("Целевая толщина стенки, мм", min_value=0.1, max_value=50.0, value=4.0, step=0.25, format="%.3f", key="cal_target_wall")
        cal_problem_h = st.number_input("С какой высоты началась проблема, мм", min_value=0.0, max_value=1000.0, value=float(_h) * 0.55, step=1.0, format="%.3f", key="cal_problem_h")
        cal_z_offset = st.number_input("Макс. ручной Z-offset, мм", min_value=-100.0, max_value=100.0, value=-10.0, step=0.5, format="%.3f", key="cal_z_offset")
        cal_test_h = st.number_input("Высота тестового G-code, мм", min_value=1.0, max_value=1000.0, value=25.0, step=1.0, format="%.3f", key="cal_test_h")
    with colg2:
        cal_feed_st = st.number_input("Feed/C override стабильная зона, %", min_value=0.0, max_value=300.0, value=100.0, step=5.0, format="%.3f", key="cal_feed_st")
        cal_wire_st = st.number_input("Wire override стабильная зона, %", min_value=0.0, max_value=500.0, value=100.0, step=5.0, format="%.3f", key="cal_wire_st")
        cal_cur_st = st.number_input("Current override стабильная зона, %", min_value=0.0, max_value=300.0, value=100.0, step=5.0, format="%.3f", key="cal_cur_st")
        cal_capture_min = st.number_input("Минимальная E2 для захвата ванны, мм/с", min_value=0.0, max_value=100.0, value=0.0, step=0.1, format="%.3f", key="cal_capture_min")
    with colg3:
        cal_feed_up = st.number_input("Feed/C override верхняя зона, %", min_value=0.0, max_value=300.0, value=100.0, step=5.0, format="%.3f", key="cal_feed_up")
        cal_wire_up = st.number_input("Wire override верхняя зона, %", min_value=0.0, max_value=500.0, value=100.0, step=5.0, format="%.3f", key="cal_wire_up")
        cal_cur_up = st.number_input("Current override верхняя зона, %", min_value=0.0, max_value=300.0, value=100.0, step=5.0, format="%.3f", key="cal_cur_up")

    profile = build_experience_calibration_profile(
        program_height_mm=cal_height,
        base_z_step_mm=cal_z,
        radius_mm=cal_r,
        cfeed_code_deg_min=cal_c_code,
        e2_code_mm_s=cal_e2_code,
        e0_code_ma=cal_e0_code,
        voltage_kv=cal_u,
        wire_diameter_mm=cal_wire_d,
        deposition_efficiency=cal_eta,
        actual_height_mm=cal_actual_h,
        outer_diameter_mm=cal_od,
        inner_diameter_mm=cal_id,
        measured_wall_mm=cal_wall_meas,
        target_wall_mm=cal_target_wall,
        problem_start_height_mm=cal_problem_h,
        max_z_offset_mm=cal_z_offset,
        feed_override_stable_pct=cal_feed_st,
        wire_override_stable_pct=cal_wire_st,
        current_override_stable_pct=cal_cur_st,
        feed_override_upper_pct=cal_feed_up,
        wire_override_upper_pct=cal_wire_up,
        current_override_upper_pct=cal_cur_up,
        capture_wire_min_mm_s=cal_capture_min,
        test_height_mm=cal_test_h,
    )
    profile_json = experience_profile_to_json(profile)
    profile_csv = experience_profile_to_csv(profile)

    st.markdown("### Расчётный профиль зон")
    st.dataframe(pd.DataFrame(profile.get("zones", [])), width="stretch")
    for w in profile.get("warnings", []) or []:
        st.warning(w)

    cola, colb, colc = st.columns(3)
    with cola:
        st.download_button("Скачать profile.json", profile_json, file_name=f"ebam_experience_profile_{APP_FILE_TAG}.json", mime="application/json")
    with colb:
        st.download_button("Скачать profile.csv", profile_csv, file_name=f"ebam_experience_profile_{APP_FILE_TAG}.csv", mime="text/csv")
    with colc:
        st.download_button("Скачать краткий отчёт .txt", "EXPERIENCE PROFILE\n\n" + profile_json, file_name=f"ebam_experience_profile_report_{APP_FILE_TAG}.txt", mime="text/plain")

    st.markdown("### Применение профиля к генератору")
    st.caption("По умолчанию E0/E2 усредняются для непрерывности. Зонные обновления можно включить отдельно: M67 применяется только после подтверждения HAL, иначе остаётся совместимый M68.")
    st.info("Приложение не трогает внешние overrides и не добавляет M49/M50. Для сравнимой калибровки рекомендуется WIRE override 100%, но ручная регулировка остаётся доступной.")
    ac1, ac2, ac3, ac4 = st.columns(4)
    with ac1:
        apply_c = st.checkbox("Применять Cfeed по зонам", value=True, key="cal_apply_c")
    with ac2:
        apply_e2 = st.checkbox("Применять E2 по зонам", value=True, key="cal_apply_e2")
    with ac3:
        apply_e0 = st.checkbox("Применять E0 по зонам", value=False, key="cal_apply_e0")
    with ac4:
        apply_z = st.checkbox("Применять Z-step по зонам", value=True, key="cal_apply_z")
    m68_inside = st.checkbox(
        "Обновлять E0/E2 на границах зон", value=False, key="cal_m68_inside",
        help="В режиме подтверждённого M67 значения синхронизируются со следующим кольцом. В совместимом M68 обновление немедленное и может нарушить сглаживание."
    )
    if st.button("Применить профиль к генератору", type="primary"):
        st.session_state["experience_profile_json"] = profile_json
        st.session_state["experience_profile_apply_now"] = True
        st.session_state["experience_profile_apply_cfeed"] = bool(apply_c)
        st.session_state["experience_profile_apply_wire"] = bool(apply_e2)
        st.session_state["experience_profile_apply_current"] = bool(apply_e0)
        st.session_state["experience_profile_apply_z_step"] = bool(apply_z)
        st.session_state["experience_profile_update_m68_at_zone_boundaries"] = bool(m68_inside)
        st.rerun()
    if st.button("Сбросить применённый профиль"):
        st.session_state["experience_profile_apply_now"] = False
        st.session_state["experience_profile_json"] = ""
        st.rerun()




with tabs[6]:
    st.subheader("📦 Профили режимов")
    st.caption("Сохрани текущие настройки как именованный профиль (пункт 4) — можно вместе с замерами "
               "валика. Потом загрузи профиль обратно и применяй в JSON-вкладке / CLI.")
    _pc1, _pc2 = st.columns(2)
    with _pc1:
        _pname = st.text_input("Имя профиля", value="Профиль 1", key="prof_name")
        _pnote = st.text_input("Заметка", value="", key="prof_note")
        _pbw = st.number_input("Ширина валика (опц.), мм", min_value=0.0, max_value=30.0, value=0.0, step=0.1, key="prof_bw")
        _pbh = st.number_input("Высота валика (опц.), мм", min_value=0.0, max_value=15.0, value=0.0, step=0.1, key="prof_bh")
        try:
            _prof_json = _gtools.make_profile(_pname, settings_to_json(settings), _pbw, _pbh, _pnote)
            st.download_button("💾 Сохранить профиль .json", _prof_json,
                               file_name=f"ebam_profile_{_pname.strip().replace(' ', '_') or 'profile'}.json",
                               mime="application/json", key="prof_save")
        except Exception as _e:
            st.warning(f"Не удалось собрать профиль: {_e}")
    with _pc2:
        _pup = st.file_uploader("Загрузить профиль .json", type=["json"], key="prof_upload")
        if _pup is not None:
            try:
                _r = _gtools.read_profile(_pup.getvalue().decode("utf-8", errors="replace"))
                if _r.get("ok"):
                    st.success(f"Профиль: {_r['name']}")
                    if _r.get("meta"):
                        st.caption(", ".join(f"{k}={v}" for k, v in _r["meta"].items() if v))
                    st.code(_r["settings_json"], language="json")
                    st.caption("Скопируй JSON во вкладку «JSON» или сохрани как ebam_settings.json для CLI.")
                else:
                    st.warning(_r.get("error", "Не удалось прочитать профиль."))
            except Exception as _e:
                st.warning(f"Ошибка чтения: {_e}")

    st.divider()
    st.subheader("📓 Журнал наплавок")
    st.caption("Записывай факт против расчёта (пункт 5). Со временем это станет твоей базой режимов. "
               "Журнал хранится в скачиваемом .json — загрузи существующий, добавь запись, сохрани снова.")
    _jup = st.file_uploader("Загрузить журнал .json (если есть)", type=["json"], key="jrnl_upload")
    _entries = []
    if _jup is not None:
        try:
            _entries = _gtools.journal_from_json(_jup.getvalue().decode("utf-8", errors="replace"))
            st.caption(f"Загружено записей: {len(_entries)}")
        except Exception as _e:
            st.warning(f"Ошибка чтения журнала: {_e}")
    with st.form("journal_form"):
        st.markdown("**Новая запись**")
        _jc1, _jc2 = st.columns(2)
        with _jc1:
            _jpart = st.text_input("Деталь / версия", value="")
            _jsha = st.text_input("SHA / имя файла", value="")
            _jverdict = st.text_input("Вердикт", value="")
            _jphotos = st.number_input("Число фото", min_value=0, max_value=99, value=0, step=1)
        with _jc2:
            st.caption("Размеры: план → факт (через дефис имя размера)")
            _jd1 = st.text_input("Размер 1 (имя)", value="OD")
            _jp1 = st.number_input("план 1", value=0.0, step=0.1, key="jp1")
            _jm1 = st.number_input("факт 1", value=0.0, step=0.1, key="jm1")
            _jd2 = st.text_input("Размер 2 (имя)", value="высота")
            _jp2 = st.number_input("план 2", value=0.0, step=0.1, key="jp2")
            _jm2 = st.number_input("факт 2", value=0.0, step=0.1, key="jm2")
        _submitted = st.form_submit_button("➕ Добавить запись в журнал")
    if _submitted and _jpart:
        _planned, _measured = {}, {}
        if _jd1:
            _planned[_jd1] = _jp1
            _measured[_jd1] = _jm1
        if _jd2:
            _planned[_jd2] = _jp2
            _measured[_jd2] = _jm2
        _entries.append(_gtools.make_journal_entry(_jpart, _jsha, _planned, _measured, _jverdict, _jphotos))
        st.success("Запись добавлена. Сохрани обновлённый журнал ниже.")
    if _entries:
        st.dataframe(pd.DataFrame([
            {"дата": e.get("date"), "деталь": e.get("part"), "SHA": e.get("sha"),
             "Δ размеры": ", ".join(f"{k}:{v:+.1f}" for k, v in (e.get("deltas") or {}).items()),
             "вердикт": e.get("verdict"), "фото": e.get("photos")}
            for e in _entries
        ]))
        st.download_button("💾 Сохранить журнал .json", _gtools.journal_to_json(_entries),
                           file_name="ebam_journal.json", mime="application/json", key="jrnl_save")

st.markdown("---")
st.caption("By Керенцев Максим")
