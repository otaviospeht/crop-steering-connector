"""Every line the connector writes to the app's Log tab.

The lines of `APPROVED`, `PUMP_APPROVED`, `HUMIDIFIER_APPROVED` and `LIGHT_APPROVED` are approved texts, kept
verbatim, and DOCS.md explains each line of `APPROVED`. The other lines carry only codes, counts and entity ids for
the Cropilot team; they never carry a token, the pairing code, a payload or a device name.
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
SENSOR_REFRESH_FAILED = (
    "Falha ao atualizar {count} sensores no Home Assistant: {detail}. Nova tentativa em 10 minutos."
)
# Once per process, at the first failed identity read; `{detail}` is a transport kind, an HTTP status or a fixed word.
DEVICE_IDENTITIES_FAILED = (
    "Falha ao ler as integrações dos aparelhos no Home Assistant: {detail}. O conector tenta de novo na próxima "
    "leitura dos aparelhos."
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
    SENSOR_REFRESH_FAILED,
    DEVICE_IDENTITIES_FAILED,
    LIGHT_FILE_UNREADABLE,
    LIGHT_EVENTS_DROPPED,
    LIGHT_NIGHT_GUARD_DONE,
)
