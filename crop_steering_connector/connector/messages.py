"""Every line the connector writes to the app's Log tab.

The lines of `APPROVED`, `PUMP_APPROVED`, `HUMIDIFIER_APPROVED`, `RESTORE_APPROVED` and `LIGHT_APPROVED` are approved
texts, kept verbatim, and DOCS.md explains each line of `APPROVED`. The other lines carry only codes, counts and entity
ids for the Cropilot team; they never carry a token, the pairing code, a payload or a device name. `PUMP_BACKUP`
(0.2.16) are the backup watering's lines: one per slot started and ended, with the entity id and the shot's key only.
`RESTORE_APPROVED` (0.2.17) are the dehumidifier's re-on lines, and `CLIMATE_BACKUP` (0.2.17) the climate reserve's,
with the entity id, the step's instant and codes only.
"""

V13_REVOKED = (
    "Pareamento revogado. Gere um código novo em Dispositivos › Conexões, no Cropilot, e cole em “Código de "
    "pareamento”."
)
V14_FORBIDDEN = "A conta no Cropilot não está liberada para o conector. Fale com a equipe do Cropilot."
V15_TOO_LARGE = (
    "O relatório passou do tamanho aceito pelo Cropilot. O próximo vai só com os aparelhos que o Cropilot usa."
)
V16_PAIRED = "Pareado com o Cropilot."
V17_WAITING_HA = "Aguardando o Home Assistant terminar de iniciar."
V33_PAIRING_CODE_REFUSED = (
    "Código de pareamento vencido ou já usado. Gere um código novo em Dispositivos › Conexões, no Cropilot, e cole em "
    "“Código de pareamento”."
)
V34_SERVER_URL_INVALID = "O “Endereço do servidor” não é aceito. Deixe o campo vazio e clique em “Reiniciar”."
V35_NO_PAIRING_CODE = (
    "Falta o código de pareamento. Gere o código em Dispositivos › Conexões, no Cropilot, e cole em “Código de "
    "pareamento”, na aba Configuração."
)
# The device map could not be read; the report goes on with the last good map, or with no device keys.
DEVICE_NAMES_FAILED = (
    "Não foi possível ler os nomes dos aparelhos no Home Assistant. O relatório segue com os nomes das entidades."
)

APPROVED = (
    V13_REVOKED,
    V14_FORBIDDEN,
    V15_TOO_LARGE,
    V16_PAIRED,
    V17_WAITING_HA,
    V33_PAIRING_CODE_REFUSED,
    V34_SERVER_URL_INVALID,
    V35_NO_PAIRING_CODE,
    DEVICE_NAMES_FAILED,
)

# The pump watch. `{entity}` is the pump's entity id.
PUMP_FORCED_OFF = "A bomba {entity} ficou ligada além do pulso. O conector desligou a tomada."
PUMP_OUTSIDE_OFF = "A bomba {entity} ligou fora de um disparo. O conector desligou a tomada."
PUMP_BOOT_OFF = "A bomba {entity} estava ligada quando o conector iniciou. O conector desligou a tomada."
PUMP_OFF_RETRY = "A bomba {entity} não desligou. O conector tenta de novo em {seconds} s."
PUMP_FILE_UNREADABLE = "A lista de bombas guardada não pôde ser lida. O conector espera a próxima resposta do Cropilot."

PUMP_APPROVED = (
    PUMP_FORCED_OFF,
    PUMP_OUTSIDE_OFF,
    PUMP_BOOT_OFF,
    PUMP_OFF_RETRY,
    PUMP_FILE_UNREADABLE,
)

# The humidifier watch. `{entity}` is the humidifier's entity id.
HUMIDIFIER_STOP_OFF = (
    "O pareamento com o Cropilot terminou. O conector desligou o umidificador {entity}, que o Cropilot tinha ligado."
)
HUMIDIFIER_PAIR_OFF = "O conector foi pareado de novo e desligou o umidificador {entity}, que o Cropilot tinha ligado."
HUMIDIFIER_SILENCE_OFF = (
    "O Cropilot não responde há 20 minutos. O conector desligou o umidificador {entity}, que o Cropilot tinha ligado."
)
HUMIDIFIER_OFF_RETRY = "O umidificador {entity} não desligou. O conector tenta de novo em {seconds} s."
HUMIDIFIER_FILE_UNREADABLE = (
    "A lista dos umidificadores que o Cropilot ligou não pôde ser lida. O conector começa com a lista vazia."
)

HUMIDIFIER_APPROVED = (
    HUMIDIFIER_STOP_OFF,
    HUMIDIFIER_PAIR_OFF,
    HUMIDIFIER_SILENCE_OFF,
    HUMIDIFIER_OFF_RETRY,
    HUMIDIFIER_FILE_UNREADABLE,
)

# The dehumidifier's re-on (0.2.17). `{entity}` is the entity id of the dehumidifier or of its plug.
RESTORE_STOP_ON = (
    "O pareamento com o Cropilot terminou. O conector religou o desumidificador {entity}, que o Cropilot tinha "
    "desligado para o umidificador trabalhar."
)
RESTORE_PAIR_ON = (
    "O conector foi pareado de novo e religou o desumidificador {entity}, que o Cropilot tinha desligado para o "
    "umidificador trabalhar."
)
RESTORE_SILENCE_ON = (
    "O Cropilot não responde há 20 minutos. O conector religou o desumidificador {entity}, que o Cropilot tinha "
    "desligado para o umidificador trabalhar."
)
RESTORE_LEASE_ON = (
    "O Cropilot não confirmou por 25 minutos o desligamento do desumidificador {entity}. O conector o religou."
)
RESTORE_ON_RETRY = "O desumidificador {entity} não religou. O conector tenta de novo em {seconds} s."

RESTORE_APPROVED = (
    RESTORE_STOP_ON,
    RESTORE_PAIR_ON,
    RESTORE_SILENCE_ON,
    RESTORE_LEASE_ON,
    RESTORE_ON_RETRY,
)

# The light controller. `{entity}` is the light plug's entity id, `{verb}` «acender» or «apagar» and `{data}` the
# end of the schedule kept, in UTC.
LIGHT_ON = "O conector acendeu a iluminação {entity} no horário da fase."
LIGHT_OFF = "O conector apagou a iluminação {entity} no horário da fase."
LIGHT_NIGHT_OFF = "A iluminação {entity} estava acesa no período de noite. O conector a apagou."
LIGHT_FAILED = (
    "O conector não conseguiu {verb} a iluminação {entity} depois de 3 tentativas. Ele continua tentando até o "
    "próximo horário."
)
LIGHT_EXPIRED = (
    "O conector ficou 7 dias sem contato com o Cropilot e parou de acender e apagar a iluminação {entity}. Ela fica "
    "como está."
)
LIGHT_STOPPED = (
    "O conector parou de acender e apagar a iluminação, porque não fala mais com o Cropilot por este pareamento. "
    "Ligue o timer da tomada no app Smart Life."
)
LIGHT_SCHEDULE_INVALID = (
    "Os horários da iluminação {entity} que chegaram do Cropilot vieram com erro. O conector segue os últimos "
    "horários válidos até {data}."
)
LIGHT_SCHEDULE_INVALID_NONE_HELD = (
    "Horários da iluminação inválidos e nenhum horário guardado: a iluminação fica como está."
)
LIGHT_REFUSED_PLUG = (
    "A tomada {entity} é de um ar-condicionado, de um desumidificador ou da bomba de rega, e o conector não a usa "
    "como iluminação."
)
LIGHT_DEVICE_UNKNOWN = (
    "O conector ainda não sabe de que aparelho do Home Assistant é a tomada {entity} e espera a próxima leitura dos "
    "aparelhos para usá-la como iluminação."
)
LIGHT_DEVICE_MISMATCH = (
    "Os horários da iluminação {entity} chegaram para outro aparelho do Home Assistant, e o conector não os usa."
)
LIGHT_DEVICE_CHANGED = (
    "A entidade {entity} agora é de outro aparelho no Home Assistant, e o conector não a usa mais como iluminação."
)
LIGHT_CLOCK_SKEW = (
    "O relógio do Home Assistant está mais de 5 minutos diferente da hora do Cropilot. O conector não acende nem "
    "apaga a iluminação até isso se acertar."
)
LIGHT_CLOCK_BACK = (
    "O relógio voltou mais de 5 minutos. O conector só acende a iluminação no próximo horário de acender."
)
LIGHT_CAPPED = (
    "O conector parou de mandar comandos à iluminação {entity} até o próximo horário, depois de muitas tentativas em "
    "uma hora."
)

LIGHT_APPROVED = (
    LIGHT_ON,
    LIGHT_OFF,
    LIGHT_NIGHT_OFF,
    LIGHT_FAILED,
    LIGHT_EXPIRED,
    LIGHT_STOPPED,
    LIGHT_SCHEDULE_INVALID,
    LIGHT_SCHEDULE_INVALID_NONE_HELD,
    LIGHT_REFUSED_PLUG,
    LIGHT_DEVICE_UNKNOWN,
    LIGHT_DEVICE_MISMATCH,
    LIGHT_DEVICE_CHANGED,
    LIGHT_CLOCK_SKEW,
    LIGHT_CLOCK_BACK,
    LIGHT_CAPPED,
)

# The backup watering (0.2.16). `{entity}` is the pump's entity id, `{date}` and `{slot}` the light date and the slot
# index of the shot, `{reason}` a code of the event and `{count}` a number; never a dose, a name or a payload.
BACKUP_SLOT_STARTED = (
    "Rega de reserva: sem resposta do Cropilot, o conector começou sozinho o disparo {slot} de {date} na bomba {entity}."
)
BACKUP_SLOT_ENDED = "Rega de reserva: o conector terminou o disparo {slot} de {date} na bomba {entity}."
BACKUP_SLOT_SKIPPED = (
    "Rega de reserva: o conector não seguiu com o disparo {slot} de {date} na bomba {entity}: {reason}."
)
BACKUP_SLOT_STOPPED = (
    "Rega de reserva: o conector parou o disparo {slot} de {date} na bomba {entity} depois do pulso em andamento."
)
BACKUP_SCHEDULE_REFUSED = (
    "Os horários da rega de reserva da bomba {entity} não foram aceitos ({reason}). O conector não rega sozinho com "
    "essa bomba até receber horários válidos do Cropilot."
)
BACKUP_EXPIRED = (
    "Os horários da rega de reserva da bomba {entity} acabaram. O conector só volta a regar sozinho com horários novos "
    "do Cropilot."
)
BACKUP_STOPPED = "O conector apagou os horários da rega de reserva e não rega sozinho até receber horários novos."
BACKUP_FILE_UNREADABLE = (
    "Os dados guardados da rega de reserva não puderam ser lidos. O conector começa sem eles."
)
BACKUP_PULSE_LOST = (
    "O conector reiniciou durante um disparo da rega de reserva na bomba {entity}, e a vigia da bomba não tinha mais "
    "esse disparo."
)
BACKUP_EVENTS_DROPPED = "Limite de 256 eventos da rega de reserva guardados: {count} eventos mais antigos descartados."
BACKUP_EVENTS_UNACKNOWLEDGED = (
    "O Cropilot não confirmou {count} eventos da rega de reserva em muitos relatórios seguidos, e o conector os "
    "descartou."
)

PUMP_BACKUP = (
    BACKUP_SLOT_STARTED,
    BACKUP_SLOT_ENDED,
    BACKUP_SLOT_SKIPPED,
    BACKUP_SLOT_STOPPED,
    BACKUP_SCHEDULE_REFUSED,
    BACKUP_EXPIRED,
    BACKUP_STOPPED,
    BACKUP_FILE_UNREADABLE,
    BACKUP_PULSE_LOST,
    BACKUP_EVENTS_DROPPED,
    BACKUP_EVENTS_UNACKNOWLEDGED,
)

# The climate reserve (0.2.17). `{entity}` is the device's entity id, `{step}` the instant of the light transition in
# UTC, `{capability}` and `{reason}` codes of the event, `{value}` the number sent and `{count}` a number.
CLIMATE_TAKEOVER = (
    "Reserva do clima: sem resposta do Cropilot, o conector segue os horários guardados do aparelho {entity}."
)
CLIMATE_APPLIED = "Reserva do clima: o conector aplicou no aparelho {entity} o horário de {step}: {capability} {value}."
CLIMATE_MODE_EXIT = "Reserva do clima: o conector passou o ar-condicionado {entity} a refrigerar."
CLIMATE_SKIPPED = "Reserva do clima: o conector não aplicou no aparelho {entity} o horário de {step}: {reason}."
CLIMATE_SCHEDULE_REFUSED = (
    "Os horários da reserva do clima do aparelho {entity} não foram aceitos ({reason}). O conector não muda esse "
    "aparelho sozinho até receber horários válidos do Cropilot."
)
CLIMATE_EXPIRED = (
    "Os horários da reserva do clima do aparelho {entity} acabaram. O conector só volta a mudar esse aparelho sozinho "
    "com horários novos do Cropilot."
)
CLIMATE_STOPPED = (
    "O conector apagou os horários da reserva do clima e não muda o ar-condicionado nem o desumidificador sozinho até "
    "receber horários novos."
)
CLIMATE_FILE_UNREADABLE = "Os dados guardados da reserva do clima não puderam ser lidos. O conector começa sem eles."
CLIMATE_EVENTS_DROPPED = (
    "Limite de 256 eventos da reserva do clima guardados: {count} eventos mais antigos descartados."
)
CLIMATE_EVENTS_UNACKNOWLEDGED = (
    "O Cropilot não confirmou {count} eventos da reserva do clima em muitos relatórios seguidos, e o conector os "
    "descartou."
)

CLIMATE_BACKUP = (
    CLIMATE_TAKEOVER,
    CLIMATE_APPLIED,
    CLIMATE_MODE_EXIT,
    CLIMATE_SKIPPED,
    CLIMATE_SCHEDULE_REFUSED,
    CLIMATE_EXPIRED,
    CLIMATE_STOPPED,
    CLIMATE_FILE_UNREADABLE,
    CLIMATE_EVENTS_DROPPED,
    CLIMATE_EVENTS_UNACKNOWLEDGED,
)

STARTED = "Conector do Crop Steering {version} iniciado."
NO_SUPERVISOR_TOKEN = (
    "O conector não recebeu o acesso ao Home Assistant (SUPERVISOR_TOKEN ausente). Clique em “Reiniciar”; "
    "se a linha voltar, mande a aba Registro à equipe do Cropilot."
)
TOKEN_WRITE_FAILED = (
    "Não foi possível guardar o pareamento neste Home Assistant: {error}. O conector segue funcionando, mas "
    "depois de reiniciar vai precisar de um código novo, que você gera em Dispositivos › Conexões, no Cropilot."
)
PAIR_STOPPED = "O Cropilot recusou o pareamento: {detail}. Mande a aba Registro à equipe do Cropilot."
ENDPOINT_FAILED = "Falha ao falar com o Cropilot ({endpoint}): {detail}. Nova tentativa em {seconds} s."
HA_READ_FAILED = "Falha ao ler o Home Assistant em {path}: {detail}."
FIRST_REPORT = (
    "Primeiro relatório aceito: {sent} aparelhos e sensores enviados, {allowed} em uso no Cropilot."
)
REPORT_TRIMMED = "Relatório reduzido pelo limite de tamanho: {count} aparelhos e sensores ficaram de fora."
REPORT_REFUSED = "Relatório recusado pelo Cropilot: {detail}. O próximo vai só com os aparelhos que o Cropilot usa."
TOO_MANY_COMMANDS = "A busca trouxe {count} comandos; só os 5 primeiros foram tratados."
INVALID_COMMAND_ID = "Comando sem identificador válido: ignorado."
DUPLICATE_COMMAND = "Comando {id} já tratado nesta execução: ignorado."
COMMAND_REFUSED = "Comando {id} ({action}, {entity}) recusado antes do início: {code}."
COMMAND_NOT_STARTED = "Comando {id} ({action}, {entity}) não iniciado pelo Cropilot: {detail}."
# 0.2.15: a command held for its `not_before`, and the claim paused while too many wait.
COMMAND_WAITING = "Comando {id} ({action}, {entity}) espera {seconds} s para começar."
WAITING_FULL = "{count} comandos esperam a hora de começar: a busca de novos comandos aguarda um deles sair."
COMMAND_DONE = "Comando {id} ({action}, {entity}) no Home Assistant: {outcome}."
RESULT_SETTLED = "Resultado do comando {id} encerrado pelo Cropilot: {detail}."
RESULT_RETRY = "Resultado do comando {id} pendente: {detail}. Nova tentativa em {seconds} s."
RESULT_NOT_SENT = "Resultado do comando {id} não enviado: {detail}."
BATCH_DROPPED = "{count} comandos do lote ficaram sem execução depois da espera do resultado."
UNEXPECTED_ERROR = "Erro inesperado ({where}): {error}."
PUMP_EVENTS_DROPPED = "Limite de 20 eventos de bomba guardados: {count} eventos mais antigos descartados."
HUMIDIFIER_EVENTS_DROPPED = (
    "Limite de 20 eventos de umidificador guardados: {count} eventos mais antigos descartados."
)
HUMIDIFIER_GONE = "O umidificador {entity} não existe mais no Home Assistant. O conector deixou de vigiá-lo."
HUMIDIFIER_OFF_UNCONFIRMED = (
    "O Home Assistant não confirmou o desligamento do umidificador {entity}. O conector tenta de novo em {seconds} s."
)
HUMIDIFIER_WATCHED_DROPPED = (
    "Limite de 200 umidificadores vigiados: {count} umidificadores mais antigos deixaram de ser vigiados."
)
# The dehumidifier's re-on (0.2.17), for the team.
RESTORE_GONE = "O desumidificador {entity} não existe mais no Home Assistant. O conector deixou de vigiá-lo."
RESTORE_ON_UNCONFIRMED = (
    "O Home Assistant não confirmou que o desumidificador {entity} religou. O conector tenta de novo em {seconds} s."
)
RESTORE_NOT_DEHUMIDIFIER = (
    "A entidade {entity} não é mais um desumidificador no Home Assistant. O conector deixou de vigiá-la e não a religa."
)
RESTORE_WATCHED_DROPPED = (
    "Limite de 200 desumidificadores vigiados: {count} desumidificadores mais antigos deixaram de ser vigiados."
)
SENSOR_REFRESH_FAILED = (
    "Falha ao atualizar {count} sensores no Home Assistant: {detail}. Nova tentativa em 10 minutos."
)
# Once per process, at the first failed identity read; `{detail}` is a transport kind, an HTTP status or a fixed word.
DEVICE_IDENTITIES_FAILED = (
    "Falha ao ler as integrações dos aparelhos no Home Assistant: {detail}. O conector tenta de novo na próxima "
    "leitura dos aparelhos."
)
# Once per process, at the first failed device info read (0.2.13); `{detail}` as above, never a value it read.
DEVICE_INFO_FAILED = (
    "Falha ao ler o fabricante e o modelo dos aparelhos no Home Assistant: {detail}. O conector tenta de novo na "
    "próxima leitura dos aparelhos."
)
LIGHT_FILE_UNREADABLE = (
    "Os horários da iluminação guardados não puderam ser lidos. O conector espera os próximos horários do Cropilot."
)
LIGHT_EVENTS_DROPPED = "Limite de 20 eventos da iluminação guardados: {count} eventos mais antigos descartados."
# After the night guard's third off in one dark period.
LIGHT_NIGHT_GUARD_DONE = (
    "O conector já apagou a iluminação {entity} 3 vezes neste período de noite e não a apaga mais até o próximo "
    "horário de acender."
)

TECHNICAL = (
    STARTED,
    NO_SUPERVISOR_TOKEN,
    TOKEN_WRITE_FAILED,
    PAIR_STOPPED,
    ENDPOINT_FAILED,
    HA_READ_FAILED,
    FIRST_REPORT,
    REPORT_TRIMMED,
    REPORT_REFUSED,
    TOO_MANY_COMMANDS,
    INVALID_COMMAND_ID,
    DUPLICATE_COMMAND,
    COMMAND_REFUSED,
    COMMAND_NOT_STARTED,
    COMMAND_DONE,
    RESULT_SETTLED,
    RESULT_RETRY,
    RESULT_NOT_SENT,
    BATCH_DROPPED,
    UNEXPECTED_ERROR,
    PUMP_EVENTS_DROPPED,
    HUMIDIFIER_EVENTS_DROPPED,
    HUMIDIFIER_GONE,
    HUMIDIFIER_OFF_UNCONFIRMED,
    HUMIDIFIER_WATCHED_DROPPED,
    RESTORE_GONE,
    RESTORE_ON_UNCONFIRMED,
    RESTORE_NOT_DEHUMIDIFIER,
    RESTORE_WATCHED_DROPPED,
    SENSOR_REFRESH_FAILED,
    DEVICE_IDENTITIES_FAILED,
    DEVICE_INFO_FAILED,
    LIGHT_FILE_UNREADABLE,
    LIGHT_EVENTS_DROPPED,
    LIGHT_NIGHT_GUARD_DONE,
)
