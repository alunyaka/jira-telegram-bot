# Импорт из нашего конфиг-файла. Перед началом работы внесите в него необходимые данные
from config_jira import JIRA_URL, TOKEN, auth, vip, team_id
# Раскомментировать на случай, если нужно использовать proxy для Telegram
# from config_jira import REQUEST_KWARGS

from datetime import datetime, timedelta
import requests
import json

from telegram.ext import Updater, CommandHandler
import logging

###############################
# наш словарь пользователей, их логины и списанное время
team_members = {}

# Настройки отправки запросов
headers = {'content-type': 'application/json; charset=UTF-8'}


# Получаем список пользователей в команде. Пока выполняется только раз при запуске бота.

def get_team():
    response = requests.get('{}/rest/tempo-teams/2/team/{}/member'.format(JIRA_URL, team_id),
                            params={'teamId': [team_id]}, headers=headers, auth=auth)
    users = json.loads(response.text)
    print(json.dumps(users, indent=4, sort_keys=True))
    for i in users:
        if i['member']['name'] in vip:
            continue
        team_members[i['member']['name']] = {'displayname': i['member']['displayname'], 'timeworked_sec': 0,
                                             'timeworked_str': ''}


def check_current_week(today):
    start_of_week = today - timedelta(days=today.weekday())  # начало текущей недели
    end_of_week = start_of_week + timedelta(days=6)  # конец текущей недели
    # отправляем в функцию дефолтное кол-во рабочих часов на неделе, ответ переводим в секунды
    week_time = check_holidays(40, today) * 3600
    # форматируем даты в понятном Jira формате
    start_of_week = start_of_week.strftime('%Y-%m-%d')
    end_of_week = end_of_week.strftime('%Y-%m-%d')

    data = json.dumps(
        {'from': start_of_week, 'to': end_of_week, 'teamId': [team_id]}
    )  # нужно спарсить json, иначе не принимает
    response = requests.post('{}/rest/tempo-timesheets/4/worklogs/search'.format(JIRA_URL),
                             data=data, headers=headers, auth=auth)
    if response.status_code != 200:
        print('Ошибка на сервере Jira!')
        return 'Ошибка на сервере Jira! Сообщите администратору'

    timesheets = json.loads(response.text)  # ответ возвращается как string, превращаем его в словарь/Json
    print(timesheets)
    for work in timesheets:
        # добавляем отработанные секунды в словарь членов команды
        if work['worker'] not in team_members:
            print('Не удалось найти сотрудника с логином ' + work['worker'])
            continue
        team_members[work['worker']]['timeworked_sec'] += work['billableSeconds']

    message = "🕑 Текущее состояние Tempo: \n"
    for i in team_members.values():
        # Формируем сообщение.
        message += i['displayname'] + '\n' + '<code>' + progress_bar(week_time, i['timeworked_sec']) + "</code>" + '\n'
        i['timeworked_sec'] = 0  # Сбрасываем время, чтобы оно при повторном вызове функции не суммировалось
    return message


def progress_bar(req_time, act_time):
    if req_time == act_time:
        # избегаем лишних операций, если уже все время сразу списано
        return '[####################' + '] {} / {}ч.'.format(act_time / 3600, req_time / 3600)
    elif req_time < act_time:
        # если вдруг кто-то списал больше времени, чем положено, на всякий случай предупреждаем
        return '[!!!!!!!!!!!!!!!!!!!!' + '] {} / {}ч.'.format(act_time / 3600, req_time / 3600)
    elif act_time == 0:
        # ... и если ещё ничего не списано вообще
        return '[                    ' + '] {} / {}ч.'.format(act_time / 3600, req_time / 3600)
    one_tick = req_time / 20
    # теперь мы знаем сколько секунд в одном делении прогресс-бара.
    progress = '['
    empty_spaces = 20
    act_time_cycle = act_time
    while act_time_cycle >= one_tick:
        progress = progress + '#'
        act_time_cycle -= one_tick
        empty_spaces -= 1
    while empty_spaces != 0:
        progress = progress + ' '
        empty_spaces -= 1
    # возвращаем прогресс-бар. Время списания на всякий случай округляем до десятичного знака, если списывали минуты.
    progress = progress + '] {} / {}ч.'.format(round(act_time / 3600, 1), req_time / 3600)
    return progress


# Проверка выходных дней
def check_holidays(week_time, today):
    start_of_week = today - timedelta(days=today.weekday())  # начало текущей недели
    end_of_week = start_of_week + timedelta(
        days=4)  # конец текущей РАБОЧЕЙ недели, чтобы исключить праздники, попадющие на выходные
    # получаем id всех справочников праздников
    holiday_ids = requests.get('{}/rest/tempo-core/1/holidayscheme/'.format(JIRA_URL), auth=auth)
    id_array = json.loads(holiday_ids.text)
    ids = []
    for i in range(len(id_array)):
        ids.append(id_array[i]['id'])

    # теперь перебираем все полученные справочники
    for i in ids:
        # получаем список фиксированных праздников
        fixed = requests.get('{}/rest/tempo-core/1/holidayscheme/{}/days/fixed'.format(JIRA_URL, i),
                             auth=auth)
        fixed = json.loads(fixed.text)
        # и плавучих праздников
        floating = requests.get(
            '{}/rest/tempo-core/1/holidayscheme/{}/days/floating'.format(JIRA_URL, i), auth=auth)
        floating = json.loads(floating.text)

        for f in fixed:  # проверяем каждый полученный день (они приходят без года)
            holiday = datetime.strptime(f['date'], '%d/%b')
            holiday = holiday.replace(year=2020)
            if start_of_week <= holiday <= end_of_week:
                week_time -= convert_holiday_duration(f['duration'])
        for f in floating:
            holiday = datetime.strptime(f['date'], '%d/%b/%y')  # а эти уже с годом
            if start_of_week <= holiday <= end_of_week:
                week_time -= convert_holiday_duration(f['duration'])

    return week_time


# Проверка продолжительности выходного. Пока подразумевается, что это либо 1 час, либо 1 день (8 часов)
def convert_holiday_duration(dur):
    if dur == '1d':
        return 8
    elif dur == '1h':
        return 1
    return 0


# Debug
get_team()
print(check_current_week(datetime.now()))
print(team_members)

# ФУНКЦИИ, ОТНОСЯЩИЕСЯ К РАБОТЕ БОТА

# Включаем логирование событий в боте
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)


def start(update, context):
    # Сообщение, отправляемое при первом запуске бота юзером
    update.message.reply_text('Hi!')


def currentweek(update, context):
    reply = check_current_week(datetime.now())
    update.message.reply_html(reply)


def lastweek(update, context):
    reply = check_current_week(datetime.now() - timedelta(days=7))
    update.message.reply_html(reply)


def error(update, context):
    # Логируем ошибки
    logger.warning('Update "%s" caused error "%s"', update, context.error)


def main():
    # Запуск бота
    updater = Updater(TOKEN,  # request_kwargs=REQUEST_KWARGS,
                      use_context=True)

    # Получаем dispatcher для регистрации хэндлеров
    dp = updater.dispatcher

    # Собственно наши команды
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("currentweek", currentweek))
    dp.add_handler(CommandHandler("lastweek", lastweek))

    # Логирование ошибок
    dp.add_error_handler(error)

    # Что-то, что нужно для его работы
    updater.start_polling()

    # Для остановки бота делаем CTRL+C в консоли
    updater.idle()


if __name__ == '__main__':
    main()
