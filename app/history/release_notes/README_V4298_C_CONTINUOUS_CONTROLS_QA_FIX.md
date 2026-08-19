# EBAM G-Code Studio v4.2.9.8 — deep interaction QA report

Дата проверки: 2026-07-08
База: v4.2.9.7 C CONTINUOUS CONTROLS
Результат: v4.2.9.8 C CONTINUOUS CONTROLS QA FIX

## Что проверялось

Проверялся режим, важный для текущих опытов:
- Поворотный стол C — кольца
- Одно кольцо, C без остановки, переход C+Z
- E0/E2 непрерывно внутри процесса
- без G4-пауз, W-ретрактов, Z-only переходов внутри непрерывного блока
- авто/ручной пересчёт E2
- пересчёт F/Cfeed/E0/E2/энергии/объёма/толщины/времени при изменении параметров

## Итог тестов

| Блок проверки | Кейсов | Успешно | Ошибок |
|---|---:|---:|---:|
| Deep interaction tests | 213 | 213 | 0 |
| Extended parameter smoke | 207 | 207 | 0 |
| Итого генераций | 420 | 420 | 0 |

Дополнительно:
- py_compile app.py/core.py/cli_generate.py/desktop_launcher.py — PASS
- compileall по пакету — PASS
- cli_generate.py --help — PASS

## Найденные ошибки до исправления

### Ошибка 1: ручная E2 = 0.000 мм/с не уважалась

Было в v4.2.9.7:
```python
if mode == "manual_constant":
    q = float(getattr(settings, "wire_feed_manual_mm_s", 0.0) or 0.0)
    if q > 0:
        return q
# иначе падало в auto
```

Если оператор выбирал ручную подачу `0.000 мм/с`, программа молча переходила в авторасчёт E2. Это опасно для диагностики: UI показывает ручной режим, а G-code использует авто-подачу.

Стало в v4.2.9.8:
```python
if mode == "manual_constant":
    q = float(getattr(settings, "wire_feed_manual_mm_s", 0.0) or 0.0)
    return max(0.0, q)
```

Проверка после исправления:
```text
manual_constant E2 = 0.000 -> M68 E2 Q0.000
manual_constant E2 = 1.000 -> M68 E2 Q1.000
manual_constant E2 = 2.000 -> M68 E2 Q2.000
PASS
```

### Ошибка 2: авто-шаг по ширине валика не применялся в C/rotational режимах

Было в v4.2.9.7:
```python
radial_step = float(getattr(settings, "rotational_radial_step_mm", 0.0)) or float(settings.hatch_spacing)
```

То есть галочка `Брать шаг дорожек из ширины валика` меняла подсказку в UI, но в C-режиме радиальный шаг всё равно мог остаться старым `hatch_spacing`.

Стало в v4.2.9.8:
```python
def _effective_rotational_radial_step(settings: ProcessSettings) -> float:
    explicit = float(getattr(settings, "rotational_radial_step_mm", 0.0) or 0.0)
    if explicit > 0.0:
        return max(explicit, 0.1)
    if bool(getattr(settings, "auto_hatch_from_bead", False)) and float(getattr(settings, "bead_width_mm", 0.0) or 0.0) > 0.0:
        model = str(getattr(settings, "overlap_model", "tom") or "tom").strip().lower()
        factor = 0.667 if model in ("fom", "flat", "flat_top", "0667", "0.667") else 0.738
        return max(float(getattr(settings, "bead_width_mm", 0.0)) * factor, 0.1)
    return max(float(getattr(settings, "hatch_spacing", 0.0) or 0.0), 0.1)
```

Проверка после исправления:
```text
bead_width=1.0, TOM -> step=0.738 мм
bead_width=2.0, TOM -> step=1.476 мм
bead_width=3.0, TOM -> step=2.214 мм
wall estimate меняется вместе с шагом
PASS
```

## Проверенные зависимости

| Параметр менялся 3 раза | Проверено, что меняется | Результат |
|---|---|---|
| Ток пучка E0 10/15/20 мА | энергия Дж/мм, Дж/мм³, G-code E0 | PASS |
| Энергия Дж/мм 80/130/180 | расчётный E0, энергия, G-code E0 | PASS |
| Линейная F 300/450/750 | Cfeed, E2 auto, время, энергия | PASS |
| Z-шаг 0.3/0.5/0.7 | число слоёв, E2 auto, время | PASS |
| Радиальный шаг 0.6/1.0/1.4 | E2 auto, расчётная толщина стенки | PASS |
| Ручная E2 2/3.3/6 | G-code E2, толщина, энергия на объём | PASS |
| C max 300/650/10000 | ограничение F, Cfeed, E2 от фактической F | PASS |
| Напряжение 50/60/70 | энергия при фиксированном токе | PASS |
| Ограничение тока 12/18/50 | клиппинг E0 в G-code | PASS |
| Угол перехода 0/17/45 | длина пути и время | PASS |
| Диаметр проволоки 1.0/1.2/1.6 | E2 auto уменьшается при большем диаметре | PASS |
| Авто-шаг от ширины валика 1/2/3 | radial step, E2, wall estimate | PASS |

## Инварианты no-pause режима

В каждом успешном no-pause кейсе проверялось:
```text
C360 count == layers_total
C+Z transition count == layers_total - 1
нет M68 внутри непрерывного G91/G90 блока
нет G4 внутри непрерывного блока
нет W-команд внутри непрерывного блока
нет Z-only движения внутри непрерывного блока
E0 включается один раз
E2 включается один раз
E0/E2 выключаются только в конце
Cfeed соответствует формуле или C-limit
E2 auto соответствует F/Z/radial_step/wire_area
```

## Вывод

После исправлений v4.2.9.8 прошла 420 генерационных проверок и компиляцию. Проверка не заменяет реальный dry run на Бормаш, но по коду и расчётной логике найденные несоответствия устранены.
