# Инструкция запуска EBAM G-Code Studio на Linux

Открыть документ → скопировать нужный блок команд → вставить в терминал.

Важно: инструкция только для запуска программы. Перед реальной наплавкой нужен preview, TEST Z10–15 мм, audit и сухой прогон без луча и проволоки.

## Что нужно заранее

| Что | Как должно быть |
|---|---|
| Linux | Debian / Ubuntu / Linux Mint или похожий Linux |
| Папка программы | Папка EBAM_Gcode_Studio_... лежит на рабочем столе |
| Интернет | Нужен только при первом запуске для установки зависимостей |
| Пароль Linux | Нужен для команды sudo apt install |

## 1. Первый запуск на новом Linux

Выполнить один раз:

```bash
cd ~/Desktop
cd EBAM*
chmod +x *.sh
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
./run_linux.sh
```

Первый запуск может идти несколько минут: создаётся .venv и ставятся зависимости. Когда появится адрес http://127.0.0.1:8501 — откройте его в браузере.

## 2. Обычный запуск после первой настройки

```bash
cd ~/Desktop
cd EBAM*
./run_linux.sh
```

## 3. Ярлык на рабочем столе

```bash
cd ~/Desktop
cd EBAM*
APP_DIR="$(pwd)"

cat > "$APP_DIR/start_ebam_desktop.sh" <<EOF
#!/usr/bin/env bash
cd "$APP_DIR"
./run_linux.sh
EOF

chmod +x "$APP_DIR/start_ebam_desktop.sh"

cat > "$HOME/Desktop/EBAM_GCode_Studio.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=EBAM G-Code Studio
Comment=Запуск EBAM G-Code Studio
Exec=$APP_DIR/start_ebam_desktop.sh
Path=$APP_DIR
Terminal=true
Icon=utilities-terminal
Categories=Development;Engineering;
EOF

chmod +x "$HOME/Desktop/EBAM_GCode_Studio.desktop"
```

Если Linux спросит, доверять ли ярлыку, выберите Allow Launching / Trust and Launch / Разрешить запуск.

## 4. Как остановить программу

В терминале, где запущена программа, нажмите:

```text
Ctrl + C
```

## 5. Если появилась ошибка

### .venv/bin/activate: No such file or directory

```bash
rm -rf .venv
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
./run_linux.sh
```

### Папка EBAM не найдена

```bash
cd ~/Desktop
ls
```

### Пароль при sudo не вводится

Это нормально: символы пароля в Linux не отображаются. Введите пароль вслепую и нажмите Enter.
