# Карта возможностей: подтверждения занятий

| Модуль | Ответственность | Зависит от |
|---|---|---|
| lesson-confirmations | Ответы ученика, одного родителя и преподавателя; сводный статус занятия | — |
| max-response-ux | Однократный ответ, удаление кнопок MAX, визуальное подтверждение | lesson-confirmations |
| lesson-card-workflow | Ответы в карточке, повторный запрос, исключение ученика, перенос | lesson-confirmations |
| notification-policy | Подтверждение за 24 часа, напоминания за 3 часа и 1 час по ролям | lesson-confirmations |
| poll-center | Только пользовательские опросы в разделе рассылок | — |
| selection-consistency | Единое переключение галочки нажатием по строке | — |

Порядок: `lesson-confirmations` → `max-response-ux` → `lesson-card-workflow` →
`notification-policy`; независимо — `poll-center`, `selection-consistency`.
