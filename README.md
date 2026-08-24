# Fragment Stars для FunPayCardinal

Плагин автоматически продаёт Telegram Stars на FunPay: получает оплаченный заказ, извлекает Telegram `username`, определяет количество звёзд, покупает их через Fragment с вашего TON-кошелька и сообщает покупателю результат.

> Проект не связан с Telegram, Fragment, FunPay или авторами FunPayCardinal. Fragment не предоставляет публичный официальный API для этого сценария, поэтому интеграция использует неофициальную библиотеку [pyfragment](https://github.com/bohd4nx/pyfragment). После изменений на Fragment плагину может понадобиться обновление.

## Что умеет плагин

- работает с актуальной системой плагинов [FunPayCardinal](https://github.com/sidor0912/FunPayCardinal);
- получает username из стандартного поля персонажа или настраиваемого поля заказа;
- если username в заказе нет, запрашивает у покупателя одно сообщение вида `@username`;
- поддерживает TON и USDT в сети TON;
- поддерживает валютные лоты, фиксированные пакеты и число звёзд из названия лота;
- обрабатывает покупки строго последовательно, чтобы не конфликтовал `seqno` TON-кошелька;
- хранит состояния в SQLite и не выдаёт один FunPay-заказ дважды;
- после неопределённого сетевого сбоя не повторяет транзакцию автоматически;
- восстанавливает собственную очередь после перезапуска и опционально подхватывает неизвестные оплаченные заказы;
- имеет безопасный `dry_run`, который проходит весь цикл без транзакции;
- позволяет аварийно остановить и продолжить очередь из Telegram;
- проверяет конфиг, соединение и балансы кошелька командой `/fragment_check`;
- позволяет администратору исправить username без ручного редактирования SQLite;
- маскирует seed, API-ключ и cookies в сообщениях об ошибках;
- показывает последние операции командой `/fragment_status`.

## Важное предупреждение по безопасности

Для подписи транзакций плагину нужна seed-фраза TON-кошелька. Это полный доступ к кошельку.

1. Создайте отдельный кошелёк только для автовыдачи.
2. Держите на нём ограниченный рабочий баланс, а не основные средства.
3. Никому не отправляйте `config.json`, seed-фразу и Fragment cookies.
4. Не добавляйте `storage/plugins/fragment_stars/config.json` в Git.
5. На Linux ограничьте права: `chmod 600 storage/plugins/fragment_stars/config.json`.
6. Перед `/fragment_retry` обязательно проверьте кошелёк и историю Fragment: предыдущая транзакция могла попасть в сеть, даже если Cardinal получил ошибку.

## Требования

- Python 3.11 или новее;
- установленный и настроенный FunPayCardinal;
- отдельный TON-кошелёк с TON либо USDT TON;
- авторизованная сессия на Fragment;
- API-ключ TonAPI с [TON Console](https://tonconsole.com/) либо ключ TonCenter;
- отдельное виртуальное окружение с `pyfragment>=2026.3.4,<2027`.

## 1. Изолированная установка pyfragment

Не устанавливайте `pyfragment` в основное окружение Cardinal. FunPayCardinal фиксирует старые версии `requests` и `requests_toolbelt`, а зависимости `pyfragment` требуют новые `requests/urllib3`. Совместная установка способна сломать запуск Cardinal.

Плагин запускает сам себя отдельным Python-процессом и передаёт параметры через stdin. Seed и cookies не появляются в командной строке процесса. По умолчанию ожидается окружение `storage/plugins/fragment_stars/venv`.

### Быстрая установка из репозитория

Скачайте репозиторий, откройте терминал в его папке и укажите путь к FunPayCardinal:

```powershell
python setup_runtime.py --cardinal-dir "C:\путь\к\FunPayCardinal"
```

Linux:

```bash
python3 setup_runtime.py --cardinal-dir /home/fpc/FunPayCardinal
```

Скрипт проверяет наличие `cardinal.py`, создаёт окружение в стандартной папке и устанавливает совместимую версию `pyfragment`. Если используете другой системный пользователь, запускайте команду от того же пользователя, от которого работает Cardinal.

Ниже приведена ручная установка, если скрипт использовать нельзя.

### Windows

Откройте PowerShell в папке FunPayCardinal и создайте отдельное окружение:

```powershell
python -m venv storage\plugins\fragment_stars\venv
storage\plugins\fragment_stars\venv\Scripts\python.exe -m pip install --upgrade pip
storage\plugins\fragment_stars\venv\Scripts\python.exe -m pip install "pyfragment>=2026.3.4,<2027"
```

### Linux после официального install-fpc.sh

Замените `fpc` на пользователя, выбранного при установке, и выполняйте команды из папки Cardinal:

```bash
cd /home/fpc/FunPayCardinal
sudo -u fpc python3 -m venv storage/plugins/fragment_stars/venv
sudo -u fpc storage/plugins/fragment_stars/venv/bin/python -m pip install --upgrade pip
sudo -u fpc storage/plugins/fragment_stars/venv/bin/python -m pip install "pyfragment>=2026.3.4,<2027"
```

### Docker

Не добавляйте `pyfragment` в основной `requirements.txt`. Добавьте в `Dockerfile` до `COPY . .`:

```dockerfile
RUN python -m venv /opt/fragment-venv \
    && /opt/fragment-venv/bin/python -m pip install --no-cache-dir "pyfragment>=2026.3.4,<2027"
```

В `storage/plugins/fragment_stars/config.json` задайте:

```json
"runtime": {
  "python_executable": "/opt/fragment-venv/bin/python",
  "helper_timeout_seconds": 180
}
```

Затем пересоберите образ:

```bash
docker compose build
docker compose up -d
```

Обычная установка через `docker compose exec ... pip install` пропадёт после пересборки образа.

## 2. Установка плагина

Есть два варианта.

### Через Telegram-панель Cardinal

1. Откройте управляющего Telegram-бота Cardinal.
2. Выполните `/menu`.
3. Выберите `🧩 Плагины` → `➕ Добавить плагин`.
4. Отправьте файл `fragment_stars.py`.
5. Перезапустите Cardinal.

### Вручную

Скопируйте `fragment_stars.py` в папку:

```text
FunPayCardinal/plugins/fragment_stars.py
```

После первого запуска появится конфиг:

```text
FunPayCardinal/storage/plugins/fragment_stars/config.json
```

Остановите Cardinal перед редактированием конфига.

## 3. Подготовка Fragment

1. Откройте [Fragment](https://fragment.com/) в отдельном браузерном профиле.
2. Авторизуйтесь через Telegram.
3. Подключите выделенный TON-кошелёк, с которого будут оплачиваться звёзды.
4. Убедитесь вручную, что этот аккаунт может открыть покупку Stars и что Fragment не требует дополнительной проверки.
5. Пополните кошелёк выбранной валютой и оставьте TON на сетевые комиссии.

### Получение Fragment cookies

В Chrome/Edge:

1. На открытой странице Fragment нажмите `F12`.
2. Откройте `Application` → `Storage` → `Cookies` → `https://fragment.com`.
3. Скопируйте значения четырёх cookies:
   - `stel_ssid`;
   - `stel_dt`;
   - `stel_token`;
   - `stel_ton_token`.
4. Вставьте значения в `storage/plugins/fragment_stars/config.json`.

Cookies периодически истекают. Если в логе появилась ошибка авторизации Fragment, получите новые значения и перезапустите Cardinal.

## 4. Настройка доступа к TON

Заполните блок `fragment`:

```json
{
  "paused": false,
  "fragment": {
    "seed": "word1 word2 ... word24",
    "api_key": "ВАШ_TONAPI_KEY",
    "cookies": {
      "stel_ssid": "...",
      "stel_dt": "...",
      "stel_token": "...",
      "stel_ton_token": "..."
    },
    "wallet_version": "V5R1",
    "api_provider": "tonapi",
    "payment_method": "ton",
    "show_sender": false,
    "timeout_seconds": 45,
    "dry_run": false
  },
  "runtime": {
    "python_executable": "",
    "helper_timeout_seconds": 180
  }
}
```

Параметры:

| Параметр | Значение |
|---|---|
| `seed` | 12 или 24 слова seed-фразы выделенного кошелька |
| `api_key` | API-ключ выбранного провайдера |
| `wallet_version` | Обычно `V5R1`; для старого кошелька может понадобиться `V4R2` |
| `api_provider` | `tonapi` или `toncenter` |
| `payment_method` | `ton` или `usdt_ton` |
| `show_sender` | Показывать ли отправителя подарка |
| `dry_run` | `true` полностью имитирует выдачу, но не вызывает покупку и не подписывает транзакцию |
| `paused` | аварийная пауза; новые подходящие заказы сохраняются, но покупки не запускаются |
| `runtime.python_executable` | путь к Python из отдельного Fragment-окружения; пустая строка включает стандартный путь |
| `runtime.helper_timeout_seconds` | общий таймаут отдельного процесса, 30–600 секунд |
| `safety.recover_unknown_paid_on_startup` | если `true`, при старте добавлять неизвестные заказы со статусом `PAID`; по умолчанию безопасно выключено |

Перед боевым запуском обязательно убедитесь, что одновременно стоят `"dry_run": false` и `"paused": false`.

### Более безопасный вариант: переменные окружения

Секреты из окружения имеют приоритет над `config.json`:

```text
FPC_FRAGMENT_SEED
FPC_FRAGMENT_API_KEY
FPC_FRAGMENT_COOKIES
```

`FPC_FRAGMENT_COOKIES` — JSON одной строкой:

```json
{"stel_ssid":"...","stel_dt":"...","stel_token":"...","stel_ton_token":"..."}
```

Для systemd добавьте переменные через защищённый `EnvironmentFile`, а не непосредственно в публичный unit-файл. Для Docker используйте `env_file` или secrets и не коммитьте файл со значениями.

## 5. Настройка лота FunPay

По умолчанию плагин обрабатывает только лоты с маркером:

```text
#fragment_stars
```

Добавьте маркер в краткое или полное описание каждого лота Telegram Stars. Это предохранитель: обычный заказ никогда не запустит покупку на Fragment случайно.

Покупатель должен указать Telegram username. Оптимальный вариант — поле заказа/имя персонажа, которое попадёт в `order.player`. Допустимы:

```text
@username
username
https://t.me/username
```

Номер телефона, ID, приватная ссылка и invite-link не принимаются. Если Cardinal не видит поля с username, плагин попросит покупателя прислать `@username` в чат FunPay.

Дополнительно можно ограничить обработку ID подкатегорий:

```json
{
  "lot_filter": {
    "require_marker": true,
    "markers": ["#fragment_stars"],
    "subcategory_ids": [1234]
  }
}
```

Если `subcategory_ids` пуст, фильтрация идёт только по маркеру.

## 6. Как определяется количество Stars

В `amount.mode` доступны четыре режима.

### `auto` — рекомендуется

- если `order.amount >= 50`, количество заказа считается количеством Stars;
- иначе плагин ищет пакет в названии/описании, например `100 Telegram Stars`, и умножает 100 на количество купленных единиц.

Примеры:

| Лот | Куплено | Результат |
|---|---:|---:|
| Валютный лот, количество 500 | 500 | 500 Stars |
| `100 Telegram Stars` | 1 | 100 Stars |
| `100 Telegram Stars` | 3 | 300 Stars |

### `order_amount`

Значение `order.amount` всегда считается числом Stars. Подходит для валютного лота.

```json
"amount": { "mode": "order_amount", "minimum": 50, "maximum": 1000000, "allowed": [] }
```

### `fixed`

Каждая единица товара равна `fixed_stars_per_unit`.

```json
"amount": {
  "mode": "fixed",
  "fixed_stars_per_unit": 100,
  "minimum": 50,
  "maximum": 1000000,
  "allowed": []
}
```

### `title`

Количество берётся из названия по `title_regex` и умножается на число единиц.

### Ограничение номиналов

Для защиты от неверно настроенных лотов задайте allowlist:

```json
"allowed": [50, 100, 250, 500, 1000]
```

Тогда никакое другое количество не будет куплено автоматически.

## 7. Первый безопасный тест

1. Включите `"dry_run": true`.
2. Используйте отдельный тестовый FunPay-лот на минимальный поддерживаемый номинал.
3. Установите `allowed` только на этот номинал, например `[50]`.
4. Проверьте наличие маркера `#fragment_stars`.
5. Перезапустите Cardinal и купите тестовый лот.
6. Убедитесь, что заказ получил статус `dry_run`, а в кошельке нет новой транзакции.
7. Заполните Fragment credentials и выполните `/fragment_check`.
8. Пополните выделенный кошелёк только на один реальный тест и комиссии.
9. Выключите `dry_run`, перезапустите Cardinal и проведите реальную минимальную покупку.
10. Проверьте получение Stars, историю Fragment и `/fragment_status`.
11. Только после этого добавляйте остальные номиналы.

`dry_run`-заказ намеренно не превращается в боевой после выключения режима. Для реального теста создайте новый заказ — так исключается случайная выдача по уже проверенному ID.

## 8. Состояния заказа

Плагин хранит журнал в `storage/plugins/fragment_stars/orders.sqlite3`.

Сохранённые в SQLite заказы со статусом `queued` восстанавливаются всегда. Неизвестные плагину заказы, появившиеся во время простоя, по умолчанию не подхватываются: это исключает повторную выдачу при первой установке.

Если после первой настройки вы хотите подхватывать такие заказы, включите:

```json
"safety": {
  "purchase_delay_seconds": 2,
  "recover_unknown_paid_on_startup": true
}
```

Тогда при старте рассматриваются только неизвестные заказы со статусом `PAID`; исторические `CLOSED` всё равно игнорируются. Включайте опцию лишь после проверки, что среди текущих `PAID` нет уже выданных вручную заказов.

| Статус | Значение |
|---|---|
| `awaiting_username` | ожидается корректный username покупателя |
| `queued` | заказ в очереди |
| `processing` | выполняется запрос/транзакция |
| `completed` | Fragment подтвердил покупку |
| `submitted` | транзакция отправлена, но финальное подтверждение Fragment не получено |
| `manual_review` | результат мог быть неопределённым; автоматического повтора нет |
| `failed` | покупка не начиналась из-за безопасно обнаруженной ошибки конфигурации/подготовки |
| `dry_run` | цикл успешно проверен, но транзакция не создавалась |

Команды Telegram-панели:

```text
/fragment_status
/fragment_status ORDER_ID
/fragment_check
/fragment_pause
/fragment_resume
/fragment_set_username ORDER_ID @username
/fragment_retry ORDER_ID
```

`/fragment_pause` не отменяет транзакцию, которая уже начала выполняться. Команда останавливает запуск следующих заказов. `/fragment_resume` возвращает сохранённые заказы в очередь.

`/fragment_set_username` сразу возвращает в очередь заказ `awaiting_username`. Для `manual_review` команда меняет только username: после проверки кошелька нужен отдельный `/fragment_retry`.

`/fragment_retry` разрешён только для `manual_review`/`failed`. Перед повтором проверьте адрес кошелька, транзакции и историю Fragment. Если предыдущая транзакция уже отправлена, ручной повтор выдаст Stars второй раз.

## 9. Типовые ошибки

### `Не найден отдельный Python для Fragment`

Не создано отдельное окружение или неверен `runtime.python_executable`. Повторите раздел 1. Не устанавливайте пакет в основной Python Cardinal.

### `ModuleNotFoundError: pyfragment` от Fragment helper

Окружение найдено, но пакет установлен в другое место. Выполните установку через полный путь из `runtime.python_executable`.

### `missing stel_*` или ошибка авторизации

Cookies отсутствуют либо истекли. Повторно войдите на Fragment и замените все четыре значения.

### `need_verify` / требуется KYC

Fragment требует дополнительную проверку. Плагин её не обходит. Завершите разрешённую проверку вручную либо остановите автоматические продажи.

### Недостаточно TON/USDT

Пополните тот же кошелёк, seed-фраза которого указана в конфиге. При USDT всё равно оставьте TON для комиссии сети.

### Username не найден

Покупателю будет предложено отправить исправленный username. Аккаунт должен иметь публичный username; телефон или Telegram ID не подходят.

### Заказ перешёл в `manual_review`

Это предохранитель. Ошибка могла произойти после отправки транзакции. Сначала проверьте историю кошелька и Fragment, затем решайте, нужен ли `/fragment_retry`.

## 10. Обновление и резервная копия

Перед обновлением сохраните:

```text
storage/plugins/fragment_stars/config.json
storage/plugins/fragment_stars/orders.sqlite3
```

Замените `plugins/fragment_stars.py`, обновите `pyfragment` внутри отдельного окружения и перезапустите Cardinal. Базу заказов не удаляйте: она защищает от повторной выдачи старых заказов.

## Проверка кода без реальной покупки

Тесты не подключаются к Fragment и не подписывают транзакции:

```bash
python -m unittest discover -s tests -v
```

## Лицензия

MIT. Использование автоматизации, учёт налогов, правила FunPay/Telegram/Fragment и риски хранения ключей остаются ответственностью владельца магазина.
