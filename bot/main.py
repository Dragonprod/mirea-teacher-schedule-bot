import datetime
import logging
import json
import requests
from InlineStep import EInlineStep
import ImportantDays
from config import TELEGRAM_TOKEN, cmstoken, grafana_token
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InlineQueryResultArticle, \
    InputTextMessageContent
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    Filters,
    MessageHandler,
    Updater,
    InlineQueryHandler,
    ChosenInlineResultHandler
)
import logging_loki


class LazyLogger:
    def __init__(self):
        self.logger = None

    def init_logger(self, grafana_token):
        if grafana_token:
            loki_handler = logging_loki.LokiHandler(
                url="https://loki.grafana.mirea.ninja/loki/api/v1/push",
                auth=("logger", grafana_token),
                tags={"app": "mirea-teacher-schedule-bot", "env": "production"},
                version="1",
            )

            self.logger = logging.getLogger("bot.handlers")
            self.logger.setLevel("INFO")
            self.logger.addHandler(loki_handler)
        else:
            self.logger = logging.getLogger("bot.handlers")
            self.logger.setLevel("INFO")

    def __getattr__(self, attr):
        if not self.logger:
            self.init_logger(grafana_token)
        return getattr(self.logger, attr)


lazy_logger = LazyLogger()

updater = Updater(TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

GETNAME, GETDAY, GETWEEK, TEACHER_CLARIFY, BACK = range(5)


# Handlers
def start(update: Update, context: CallbackContext) -> int:
    """
    Привествие бота при использовании команды /start
    """
    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Привет!\nЯ бот, который поможет тебе найти "
             "расписание любого *преподавателя.*\nНапиши мне "
             "его фамилию "
             "в формате:\n*Фамилия* или *Фамилия И.О.*\n\nВ начале семестра некоторые преподаватели могут "
             "отсутствовать в расписании",
        parse_mode="Markdown")

    # Переключаемся в состояние GETNAME (ожидание ввода фамилии)
    return GETNAME


def got_name_handler(update: Update, context: CallbackContext) -> int:
    """
    Реакция бота на получение фамилии преподавателя при состоянии GETNAME
    :param update - Update класс API
    :param context - CallbackContext класс API
    :return: int сигнатура следующего состояния
    """
    inputted_teacher = update.message.text
    lazy_logger.info(json.dumps(
        {"type": "request", "query": inputted_teacher.lower(), **update.message.from_user.to_dict()}, ensure_ascii=False
    )
    )
    if len(inputted_teacher) < 2:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Слишком короткий запрос\nПопробуйте еще раз")
        return GETNAME
    teacher = normalize_teachername(inputted_teacher)

    # Устанавливаем расписание преподавателей в контексте для избежания повторных запросов
    teacher_schedule = fetch_schedule_by_name(teacher)
    if teacher_schedule:

        context.user_data["schedule"] = teacher_schedule
        available_teachers = check_same_surnames(teacher_schedule, teacher)

        if len(available_teachers) > 1:
            context.user_data["available_teachers"] = available_teachers
            return send_teacher_clarity(update, context, True)

        elif len(available_teachers) == 0:
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Ошибка при определении ФИО преподавателя. Повторите попытку, изменив запрос.\n" +
                     "Например введите только фамилию преподавателя."
            )
            return GETNAME

        else:
            context.user_data["available_teachers"] = None
            context.user_data['teacher'] = available_teachers[0]
            return send_week_selector(update, context, True)

    else:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Преподаватель не найден\nПопробуйте еще раз\n\nВ начале семестра некоторые преподаватели могут "
                 "отсутствовать в расписании")
        return GETNAME


def got_teacher_clarification_handler(update: Update, context: CallbackContext):
    """
    Реакция бота на получение фамилии преподавателя при уточнении, при состоянии TEACHER_CLARIFY
    @param update: Update class of API
    @param context: CallbackContext of API
    @return: Int код шага
    """
    chosed_teacher = update.callback_query.data
    context.user_data['teacher'] = chosed_teacher
    clarified_schedule = fetch_schedule_by_name(chosed_teacher)
    context.user_data['schedule'] = clarified_schedule
    if chosed_teacher == "back":
        return resend_name_input(update, context)
    return send_week_selector(update, context)


def got_week_handler(update: Update, context: CallbackContext) -> int:
    """
    Реакция бота на получение информации о выбранной недели в состоянии GETWEEK
    @param update: Update class of API
    @param context: CallbackContext of API
    @return: Int код шага
    """
    selected_button = update.callback_query.data
    if selected_button == "back":
        if context.user_data['available_teachers'] is not None:
            return send_teacher_clarity(update, context)
        else:
            return resend_name_input(update, context)

    elif selected_button == "today" or selected_button == "tomorrow":
        today = datetime.date.today().weekday()
        req = requests.get("https://schedule.mirea.ninja/api/schedule/current_week").json()
        week = req["week"]
        if selected_button == "tomorrow":
            if today == 6:
                week += 1  # Корректировка недели, в случае если происходит переход недели
            today = (datetime.date.today() + datetime.timedelta(days=1)).weekday()
        if today == 6:
            update.callback_query.answer("В выбранный день пар нет")
            return GETWEEK
        today += 1  # Корректировка дня с 0=пн на 1=пн
        context.user_data["week"] = week
        context.user_data["day"] = today
        return send_result(update, context)

    else:
        selected_week = int(selected_button)
        context.user_data["week"] = selected_week
        return send_day_selector(update, context)


def got_day_handler(update: Update, context: CallbackContext):
    """
    Реакция бота на выбор дня недели, предоставленный пользователю, в состоянии GETDAY
    @param update: Update class of API
    @param context: CallbackContext of API
    @return: Int код шага
    """
    selected_button = update.callback_query.data
    if selected_button == "chill":
        update.callback_query.answer(text="В этот день пар нет.", show_alert=True)
        return GETDAY
    if selected_button == "back":
        return send_week_selector(update, context)
    selected_day = -1
    if selected_button != "week":
        selected_day = int(selected_button)
    context.user_data["day"] = selected_day
    send_result(update, context)
    return BACK


# End Handlers
def normalize_teachername(raw_teacher_name: str):
    """
    Нормализация фамилии для уточнения.
    @param raw_teacher_name: Ввод пользователя
    @return: Фамилия начинаяющая с большой буквы и с пробелом в конце
    """
    teacher = raw_teacher_name.title()
    if " " not in teacher:
        teacher += " "
    return teacher


def fetch_schedule_by_name(teacher_name):
    """
    Получение информации о расписании через API Mirea Ninja
    @param teacher_name: Имя преподавателя
    @return: JSON расписание или None если преподаватель не найден
    """
    url = f"https://timetable.mirea.ru/api/teacher/search/{teacher_name}"
    response = requests.get(url)
    return response.json() if response.status_code == 200 else None


def send_week_selector(update: Update, context: CallbackContext, firsttime=False):
    """
    Отправка селектора недели. По умолчанию изменяет предыдущее сообщение, но при firsttime=True отправляет в виде нового сообщения
    @param update: Update class of API
    @param context: CallbackContext of API
    @param firsttime: Впервые ли производится общение с пользователем
    @return: Статус следующего шага - GETWEEK
    """
    teacher = ", ".join(decode_teachers([context.user_data["teacher"]]))
    if firsttime:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Выбран преподаватель: {teacher}\n" +
                 f"Выберите неделю",
            reply_markup=construct_weeks_markup()
        )

    else:
        update.callback_query.edit_message_text(
            text=f"Выбран преподаватель: {teacher}\n" +
                 f"Выберите неделю",
            reply_markup=construct_weeks_markup()
        )

    return GETWEEK


def resend_name_input(update: Update, context: CallbackContext):
    """
    Просит ввести имя преподавателя заново
    @param update: Update class of API
    @param context: CallbackContext of API
    @return: Статус следующего шага - GETNAME
    """
    update.callback_query.edit_message_text(
        text=f"Введите снова нужную фамлию преподавателя."
    )
    return GETNAME


def send_teacher_clarity(update: Update, context: CallbackContext, firsttime=False):
    """
    Отправляет список обнаруженных преподавателей. В случае если общение с пользователем не впервые - редактирует сообщение, иначе отправляет новое.
    @param update: Update class of API
    @param context: CallbackContext of API
    @param firsttime: Впервые ли производится общение с пользователем
    @return: Статус следующего шага - TEACHER_CLARIFY
    """
    available_teachers = context.user_data["available_teachers"]
    few_teachers_markup = prepare_teacher_markup(available_teachers)
    if firsttime:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Выберите преподвателя",
            reply_markup=few_teachers_markup
        )
    else:
        update.callback_query.edit_message_text(
            text="Выберите преподвателя",
            reply_markup=few_teachers_markup
        )
    return TEACHER_CLARIFY


def send_day_selector(update: Update, context: CallbackContext):
    """
    Отправляет селектор дня недели с указанием дней, когда преподаватель не имеет пар.
    @param update: Update class of API
    @param context: CallbackContext of API
    @return: Статус следующего шага - GETDAY
    """
    teacher = context.user_data["teacher"]
    week = context.user_data["week"]
    schedule = context.user_data["schedule"]
    teacher_workdays = construct_teacher_workdays(teacher, week, schedule)
    update.callback_query.edit_message_text(
        text=f"Выбран преподаватель: {teacher} \n" +
             f"Выбрана неделя: {week} \n" +
             f"Выберите день",
        reply_markup=teacher_workdays
    )
    return GETDAY


def send_result(update: Update, context: CallbackContext):
    """
    Выводит результат пользователю.
    В user_data["week"] и user_data["day"] должны быть заполнены перед вызовом!
    Если user_data["week"]=-1 - выводится вся неделя
    """
    week = context.user_data["week"]
    weekday = context.user_data["day"]
    schedule_data = context.user_data["schedule"]
    teacher_surname = context.user_data["teacher"]

    parsed_schedule = parse(schedule_data, weekday, week, teacher_surname, context)
    parsed_schedule = remove_duplicates_merge_groups_with_same_lesson(parsed_schedule)
    parsed_schedule = merge_weeks_numbers(parsed_schedule)
    if len(parsed_schedule) == 0:
        update.callback_query.answer(text="В этот день пар нет.", show_alert=True)
        return GETWEEK
    # Отправляем расписание преподавателя
    blocks_of_text = format_outputs(parsed_schedule, context)

    return telegram_delivery_optimisation(blocks_of_text, update, context)


def check_same_surnames(teacher_schedule, surname):
    """
    Проверяет имеющихся в JSON преподавателей.
    В случае нахождения однофамильца, но сдругим именем или фамилией заносит в список surnames
    :param teacher_schedule: JSON строка расписания
    :param surname: Строка фильтрации, например фамилия
    :return: surnames - лист ФИО преподавателей
    """
    surnames = []
    for teacher in teacher_schedule:
        if surname in teacher['name']:
            surnames.append(teacher['name'])
    return surnames


def construct_teacher_workdays(teacher: str, week: int, schedule: list):
    """
    Создает Inline клавиатуру с днями недели, когда у преподавателя есть пары.
    В случае если у преподавателя есть пары, то колбэк кнопки равен дню недели
    В случае если пар нет, то колбэк кнопки равен 'chill'
    @param teacher: Имя преподавателя
    @param week: Номер недели
    @param schedule: Расписание в JSON
    @return: InlineKeyboard со стилизованными кнопками
    """

    founded_days = list(
        {lesson['weekday'] for teacher in schedule for lesson in teacher['lessons'] if week in lesson['weeks']})

    no_work_indicator = "🏖️"
    weekdays = {
        1: "ПН",
        2: "ВТ",
        3: "СР",
        4: "ЧТ",
        5: "ПТ",
        6: "СБ",
    }
    ready_markup = InlineKeyboardMarkup([])
    row_list = []
    for i in range(1, 7):
        sign = ""
        callback = i
        if i not in founded_days:
            sign = "⛔"
            callback = "chill"
        row_list.append(InlineKeyboardButton(text=f"{sign}{weekdays[i]}{sign}", callback_data=callback))
        if i % 3 == 0:
            ready_markup.inline_keyboard.append(row_list)
            row_list = []
    if founded_days:  # добавляем кнопку "На неделю" только если есть пары на неделе
        row_list.append(InlineKeyboardButton(text="На неделю", callback_data="week"))
        ready_markup.inline_keyboard.append(row_list)
    row_list = []
    row_list.append(InlineKeyboardButton(text="Назад", callback_data="back"))
    ready_markup.inline_keyboard.append(row_list)
    return ready_markup


def decode_teachers(rawNames):
    """
    Декодирует ФИО преподавателей используя API CMS
    :param rawNames: список необработанных ФИО
    """
    headers = {
        "Authorization": f"Bearer {cmstoken}"}
    params = {"rawNames": ",".join(rawNames)}

    response = requests.get("https://cms.mirea.ninja/api/get-full-teacher-name", headers=headers, params=params)
    if response.status_code == 200:
        data = response.json()

        decoded_names = []
        for names in data:
            if len(names["possibleFullNames"]) == 1:
                decomposed_name = names["possibleFullNames"][0]
                name = []
                if surname := decomposed_name.get("lastName"):
                    name.append(surname)
                if first_name := decomposed_name.get("firstName"):
                    name.append(first_name)
                if middle_name := decomposed_name.get("middleName"):
                    name.append(middle_name)
                name = " ".join(name)
            else:
                name = names["rawName"]
            decoded_names.append(name)

        decoded_names = decoded_names
    else:
        decoded_names = rawNames
    return decoded_names


def prepare_teacher_markup(teachers):
    """
    Конструирует клавиатуру доступных преподавателей однофамильцев
    :param teachers: лист преподавателей
    """
    rawNames = teachers
    decoded_names = decode_teachers(rawNames)

    btns = []

    for rawName, decoded_name in zip(rawNames, decoded_names):
        btns = btns + [[InlineKeyboardButton(decoded_name, callback_data=rawName)]]
    btns = btns + [[(InlineKeyboardButton("Назад", callback_data="back"))]]
    TEACHER_CLARIFY_MARKUP = InlineKeyboardMarkup(btns)
    return TEACHER_CLARIFY_MARKUP


def construct_weeks_markup():
    """
    Создает KeyboardMarkup со списком недель, а также подставляет эмодзи
    если текущий день соответствует некоторой памятной дате+-интервал
    """
    req = requests.get("https://schedule.mirea.ninja/api/schedule/current_week").json()
    current_week = req["week"]
    week_indicator = "●"
    today = datetime.date.today()
    for day in ImportantDays.important_days:
        if abs((day[ImportantDays.DATE] - today).days) <= day[ImportantDays.INTERVAL]:
            week_indicator = day[ImportantDays.SIGN]

    reply_mark = InlineKeyboardMarkup([])
    button_list = []
    for i in range(1, 18):
        tmp_sign = ""
        if current_week == i:
            tmp_sign = week_indicator
        button_list.append(InlineKeyboardButton(text=f"{tmp_sign}{i}{tmp_sign}", callback_data=i))
        if i % 4 == 0 or i == 17:
            reply_mark.inline_keyboard.append(button_list)
            button_list = []
    backspace = []
    backspace.append(InlineKeyboardButton(text="Сегодня", callback_data="today"))
    backspace.append(InlineKeyboardButton(text="Завтра", callback_data="tomorrow"))
    reply_mark.inline_keyboard.append(backspace)
    backspace = []
    backspace.append(InlineKeyboardButton(text="Назад", callback_data="back"))
    reply_mark.inline_keyboard.append(backspace)
    return reply_mark


def parse(teacher_schedule, weekday, week_number, teacher, context):
    context.user_data["teacher"] = teacher
    for lesson in teacher_schedule:
        teacher_schedule = lesson["lessons"]
        teacher_schedule = sorted(teacher_schedule,
                                  key=lambda lesson: (
                                      lesson['weekday'], lesson['calls']['num'], lesson['group']['name']),
                                  reverse=False)
        if (weekday != -1):
            teacher_schedule = list(filter(lambda lesson: lesson['weekday'] == int(weekday), teacher_schedule))
        teacher_schedule = list(filter(lambda x: int(week_number) in x['weeks'], teacher_schedule))
        return teacher_schedule


def remove_duplicates_merge_groups_with_same_lesson(teacher_schedule):
    remove_index = []
    for i in range(len(teacher_schedule)):
        for j in range(i + 1, len(teacher_schedule)):
            if (
                    teacher_schedule[i]['calls']['num'] == teacher_schedule[j]['calls']['num'] and
                    teacher_schedule[i]['weeks'] == teacher_schedule[j]['weeks'] and
                    teacher_schedule[i]['weekday'] == teacher_schedule[j]['weekday']
            ):
                teacher_schedule[i]["group"]["name"] += ", " + teacher_schedule[j]["group"]["name"]
                remove_index.append(j)
    remove_index = set(remove_index)
    for i in sorted(remove_index, reverse=True):
        del teacher_schedule[i]
    return teacher_schedule


def merge_weeks_numbers(teacher_schedule):
    for i in range(len(teacher_schedule)):
        if teacher_schedule[i]['weeks'] == list(range(1, 18)):
            teacher_schedule[i]['weeks'] = "все"
        elif teacher_schedule[i]['weeks'] == list(range(2, 19, 2)):
            teacher_schedule[i]['weeks'] = "по чётным"
        elif teacher_schedule[i]['weeks'] == list(range(1, 18, 2)):
            teacher_schedule[i]['weeks'] = "по нечётным"
        else:
            teacher_schedule[i]['weeks'] = ", ".join(str(week) for week in teacher_schedule[i]['weeks'])
    return teacher_schedule


def format_outputs(parsed_schedule, context):
    from datetime import datetime
    text = ""
    WEEKDAYS = {
        1: "Понедельник",
        2: "Вторник",
        3: "Среда",
        4: "Четверг",
        5: "Пятница",
        6: "Суббота",
    }
    blocks = []
    for schedule in parsed_schedule:
        try:
            room = schedule["room"]["name"]
            campus = schedule["room"]["campus"]["short_name"] if schedule["room"]["campus"] and \
                                                                 schedule["room"]["campus"][
                                                                     "short_name"] else ""
            if campus != "":
                room = f"{room} ({campus})"
            else:
                room = f"{room}"

            weekday = WEEKDAYS[schedule["weekday"]]
            teachers = ", ".join(decode_teachers([context.user_data["teacher"]]))

            time_start = datetime.strptime(schedule['calls']['time_start'], "%H:%M:%S").strftime("%H:%M")
            time_end = datetime.strptime(schedule['calls']['time_end'], "%H:%M:%S").strftime("%H:%M")

            formatted_time = f"{time_start} - {time_end}"

            text += f'📝 Пара № {schedule["calls"]["num"]} в ⏰ {formatted_time}\n'
            text += f'📝 {schedule["discipline"]["name"]}\n'
            text += f'👥 Группы: {schedule["group"]["name"]}\n'
            text += f'📚 Тип: {schedule["lesson_type"]["name"]}\n'
            text += f"👨🏻‍🏫 Преподаватели: {teachers}\n"
            text += f"🏫 Аудитории: {room}\n"
            text += f'📅 Недели: {schedule["weeks"]}\n'
            text += f"📆 День недели: {weekday}\n\n"
            blocks.append(text)
            text = ""
        except:
            text += "Ошибка при получении расписания, сообщите об этом администрации в чате " \
                    "https://t.me/mirea_ninja_chat"
            blocks.append(text)
            text = ""

    return blocks


def telegram_delivery_optimisation(blocks: list, update: Update, context: CallbackContext):
    text = ""
    first = True
    for id, block in enumerate(blocks):
        text += block
        if len(text + block) >= 4096 or len(blocks) - 1 == id:
            if first:
                back_button = InlineKeyboardMarkup([[InlineKeyboardButton(text="Назад", callback_data="back")]])
                update.callback_query.edit_message_text(text, reply_markup=back_button)
                first = False
            else:
                context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=text
                )
            text = ""
    return BACK


def got_back_handler(update: Update, context: CallbackContext):
    query = update.callback_query.data
    if query == "back":
        # Заново получаем расписание, т.к. список недель был заменён на строки "По чётным", "По нечётным" и т.д.
        context.user_data["schedule"] = fetch_schedule_by_name(context.user_data["teacher"])

        return send_week_selector(update, context)


def main():
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start, run_async=True),
            MessageHandler(Filters.text & ~Filters.command, got_name_handler, run_async=True),
        ],
        states={
            GETNAME: [MessageHandler(Filters.text & ~Filters.command, got_name_handler, run_async=True)],
            GETDAY: [CallbackQueryHandler(got_day_handler, run_async=True)],
            GETWEEK: [CallbackQueryHandler(got_week_handler, run_async=True)],
            TEACHER_CLARIFY: [CallbackQueryHandler(got_teacher_clarification_handler, run_async=True)],
            BACK: [CallbackQueryHandler(got_back_handler, run_async=True)],
        },
        fallbacks=[
            CommandHandler("start", start, run_async=True),
            MessageHandler(Filters.text & ~Filters.command, got_name_handler, run_async=True),
        ],
    )

    dispatcher.add_handler(conv_handler)
    dispatcher.add_handler(CommandHandler("help", start, run_async=True))
    updater.start_polling()


if __name__ == "__main__":
    main()
