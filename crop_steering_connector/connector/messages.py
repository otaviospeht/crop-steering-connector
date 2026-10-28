"""Every line the connector writes to the app's Log tab.

The lines of `APPROVED` and `PUMP_APPROVED` are approved texts, kept verbatim, and DOCS.md explains each line
of `APPROVED`. The other lines carry only codes, counts and entity ids for the Cropilot team; they never carry
a token, the pairing code, a payload or a device name.
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
SENSOR_REFRESH_FAILED = (
    "Falha ao atualizar {count} sensores no Home Assistant: {detail}. Nova tentativa em 10 minutos."
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
    SENSOR_REFRESH_FAILED,
)
