# Conector do Crop Steering

O Conector do Crop Steering liga este Home Assistant ao Cropilot. Com ele, o Cropilot lê os sensores de temperatura e
umidade e comanda o ar-condicionado, o desumidificador, o umidificador e as tomadas com função num espaço.

## O que ele faz e o que não faz

- Ele só abre conexões de saída, com HTTPS, até o Cropilot. Ele não abre nenhuma porta no computador nem na sua rede,
  e o Cropilot nunca entra na sua rede.
- Ele não tem página: não aparece na barra lateral e não tem tela para abrir. O que ele faz aparece na aba Registro.
- Ele manda ao Cropilot só os sensores de temperatura, umidade, potência e energia, os aparelhos de clima (como o
  ar-condicionado), os umidificadores, os desumidificadores e as tomadas deste Home Assistant, com o nome de cada
  aparelho. Fechaduras, alarmes, câmeras e o resto ficam de fora. Dos desumidificadores e umidificadores, ele manda
  também os avisos de problema que o Home Assistant mostra no próprio aparelho, como o tanque cheio, só para leitura.
  Quando um desses aparelhos informa a bateria no
  Home Assistant, ele manda também o nível dela, para o Cropilot avisar quando a bateria de um sensor fica fraca.
  Para cada aparelho, ele manda também o nome das integrações do Home Assistant em que o aparelho está e um código
  embaralhado de cada uma, feito a partir do código que a integração dá ao aparelho, para o Cropilot reconhecer o mesmo
  aparelho compatível com Smart Life se o Home Assistant for reinstalado. O Cropilot não guarda esses códigos: usa só o
  dos aparelhos compatíveis com Smart Life, embaralhado de novo, e descarta os outros na hora.
- A cada 10 minutos, ele pede ao Home Assistant uma leitura nova dos sensores de temperatura e umidade e dos aparelhos
  de clima que o Cropilot usa. Muitos aparelhos só mandam um valor quando ele muda, e sem esse pedido um valor que
  fica igual por um tempo deixaria de valer no Cropilot. O pedido só atualiza a leitura: não liga, não desliga e não
  muda nenhum aparelho. Ele também diz ao Cropilot há quanto tempo cada um desses valores não muda. Assim, o Cropilot
  mostra quando um valor fica igual por muito tempo, sem deixar de usar a leitura.
- Os aparelhos que o Cropilot sabe usar entram sozinhos em Dispositivos, no Cropilot. Lá, “Tirar” tira um aparelho
  da lista, e o Cropilot para de ler e de comandar esse aparelho. “Liberar” traz o aparelho de volta.
- O Cropilot só comanda uma tomada que tem função num espaço: iluminação, exaustor, bomba de rega ou desumidificador. Sem
  função, ele só lê o estado dela, a não ser para desligar a bomba por segurança. O ar-condicionado, o desumidificador
  e a tomada escolhida como desumidificador do espaço seguem a proteção de 10 minutos do Cropilot.
- No desumidificador que aceita umidade programada, o Cropilot muda essa umidade em vez de ligar e desligar o aparelho.
- O exaustor entra numa tomada só para conferir os pulsos que você programa no timer de ciclo dela, no app Smart Life:
  o Cropilot nunca liga nem desliga o exaustor sozinho. A bomba de rega entra só pela rega automática, com o pulso
  gravado na tomada: veja “Bomba de rega”. O umidificador entra só na função Umidificador de um espaço: veja
  “Umidificador”.
- O Cropilot não guarda a senha do Smart Life nem a de nenhuma conta dos seus aparelhos: as contas ficam
  só neste Home Assistant.
- Ele não precisa de nenhum serviço pago.
- Use um Home Assistant só do cultivo: os aparelhos dele entram sozinhos no Cropilot, e uma tomada com função num
  espaço pode ser ligada e desligada pelo Cropilot.
- Para o Cropilot ler e comandar os aparelhos, o computador precisa ficar ligado e com internet, e o conector,
  iniciado.

## Instalar

1. Clique em “Instalar”. A instalação leva alguns minutos.
2. Na aba Informações, deixe ligados “Iniciar na inicialização” e “Watchdog”. Assim o conector inicia junto com o Home
   Assistant e volta sozinho se parar por um erro.

## Parear com o Cropilot

1. No Cropilot, em Dispositivos › Conexões, clique em “Gerar código”. O código aparece uma vez, vale 15 minutos e serve
   para um pareamento só.
2. Na aba Configuração, cole o código em “Código de pareamento”.
3. Clique em “Salvar”.
4. Na aba Informações, clique em “Iniciar”. Se o conector já estava iniciado, clique em “Reiniciar”.
5. Na aba Registro, confira a linha **Pareado com o Cropilot.**

Depois do pareamento, o código não é mais usado e pode ficar no campo.

## Tomadas

A tomada que o Home Assistant mostra no aparelho de um ar-condicionado ou de um desumidificador não pode ser a iluminação, o
exaustor, a bomba de rega nem o desumidificador do espaço: o Cropilot só lê o estado dela e comanda o próprio
aparelho. Um desumidificador simples ligado numa tomada inteligente entra pela tomada: escolhida como o
desumidificador do espaço, ela liga e desliga com a proteção de 10 minutos enquanto estiver nessa função. Não escolha
essa tomada como iluminação nem como bomba de rega. Na iluminação, no exaustor e na bomba de rega, a tomada liga e desliga como uma
tomada simples, sem essa proteção. Um ar-condicionado ligado numa tomada inteligente aparece como uma tomada simples:
não ponha essa tomada em nenhuma função de um espaço.

## Bomba de rega

O Cropilot liga a tomada da bomba nos horários da grade. Quem desliga é a própria tomada, no fim do pulso que você
grava no app Smart Life. A bomba não é usada à mão: o conector confere cada disparo e tenta desligar a tomada se a
bomba ficar ligada além do pulso ou ligar fora de um disparo, e o Cropilot para a rega automática e avisa você. Ao
iniciar, o conector tenta desligar a bomba que encontrar ligada fora de um disparo. A conferência continua sem contato
com o Cropilot, mas o desligamento passa pela nuvem do Smart Life: sem internet, só o pulso gravado na tomada desliga a
bomba.

Cada disparo vale pelo pulso que o Cropilot tem na hora em que o manda. Logo depois de você mudar o pulso, o primeiro
disparo, o de teste incluído, já segue o pulso novo.

A bomba também pode ser uma válvula do Home Assistant, com o conector 0.2.12 ou mais novo: o Cropilot abre a válvula
nos disparos, e ela segue as mesmas regras da tomada da bomba. Quem fecha é o temporizador da própria válvula; se ela
ficar aberta além do pulso ou abrir fora de um disparo, o conector a fecha. Enquanto ela aparece fechando, o conector
espera até 30 segundos antes de tentar de novo. Antes de voltar o conector para a 0.2.11, tire a válvula da função
Rega: a versão anterior não reconhece válvulas e descarta tudo o que a vigia da bomba guardou.

O conector 0.2.12 também sabe desligar a bomba na dose exata de cada horário, deixando o pulso gravado na tomada só
como reserva. Isso vem desligado e só passa a valer quando o Cropilot avisar: até lá, quem desliga a bomba continua
sendo o pulso da tomada.

Com o conector 0.2.12 ou mais novo, o Cropilot lê na tomada o estado ao ligar, quando o Home Assistant o mostra: ele
deve ficar em “Desligado” no app Smart Life, para a bomba não ligar sozinha quando a energia volta.

## Umidificador

O Cropilot liga o umidificador abaixo da umidade mínima da fase e desliga no ponto definido para a fase, só na germinação, na
clonagem e no vegetativo. Deixe a umidade do próprio aparelho no máximo, para quem decide ser o Cropilot. Só um
umidificador do Home Assistant entra nessa função, e ela pede o conector 0.2.7 ou mais novo.

O Conector desliga o umidificador que o Cropilot ligou quando a plataforma para de responder por 20 minutos ou quando
a conexão é revogada. Ele também desliga esse umidificador quando é pareado de novo, e avisa o Cropilot de cada
desligamento; o aviso do desligamento na revogação só chega no próximo pareamento. A exclusão da conta vale como
revogação: o Conector desliga o umidificador, e, com o Home Assistant fora do ar, é você quem precisa desligá-lo à mão.
Um umidificador que você liga no Home Assistant ou no próprio aparelho fica como está. Com um umidificador do Smart
Life, o desligamento passa pela nuvem do Smart Life: sem internet, ele pode não desligar.

## Iluminação

O Cropilot pode acender e apagar a iluminação do espaço nos horários da fase, sem o timer da tomada. Você liga isso no
espaço, no Cropilot, e desliga o timer da tomada no app Smart Life e qualquer Agenda do Home Assistant que acenda ou
apague essa tomada. O Conector guarda os horários e segue com eles por até 7 dias se perder o contato com o Cropilot;
depois disso, ele para e deixa a iluminação como está. A tomada Smart Life só recebe o comando pela internet: sem
internet ou com o Home Assistant desligado, a iluminação fica como está até eles voltarem. O timer da própria tomada
funciona sem internet.

## Segurança

- A página do conector mostra a nota de segurança 8, numa escala de 1 a 8. Antes da instalação, a loja mostra 7,
  porque o perfil de proteção do conector só conta depois que ele é instalado.
- Não passe o código de pareamento para ninguém: quem tiver o código liga outro Home Assistant à sua conta.
- O pareamento fica guardado só neste Home Assistant, e a aba Registro nunca mostra o código nem o pareamento.
- As cópias de segurança do Home Assistant não levam o pareamento. Depois de restaurar uma cópia, gere um código novo
  em Dispositivos › Conexões, no Cropilot, cole em “Código de pareamento”, clique em “Salvar” e depois em “Reiniciar”.
- Se este computador ou este Home Assistant passar para outra pessoa, desconecte o Home Assistant em
  Dispositivos › Conexões, no Cropilot.

## Mensagens da aba Registro

- **Aguardando o Home Assistant terminar de iniciar.** O Home Assistant ainda está iniciando. Não precisa fazer nada:
  o conector continua sozinho.
- **Pareado com o Cropilot.** Tudo certo: o conector passa a mandar os aparelhos ao Cropilot e a receber os comandos.
- **Falta o código de pareamento. Gere o código em Dispositivos › Conexões, no Cropilot, e cole em “Código de
  pareamento”, na aba Configuração.** Depois de colar, clique em “Salvar” e depois em “Reiniciar”.
- **Código de pareamento vencido ou já usado. Gere um código novo em Dispositivos › Conexões, no Cropilot, e cole em
  “Código de pareamento”.** Cole o código novo no lugar do antigo, clique em “Salvar” e depois em “Reiniciar”.
- **Pareamento revogado. Gere um código novo em Dispositivos › Conexões, no Cropilot, e cole em “Código de
  pareamento”.** O pareamento foi desfeito no Cropilot, e as leituras e os comandos pararam. A exclusão da conta também
  desfaz o pareamento. Com o código novo, clique em “Salvar” e depois em “Reiniciar”.
- **A conta no Cropilot não está liberada para o conector. Fale com a equipe do Cropilot.** O conector tenta de novo
  sozinho, de tempos em tempos.
- **O relatório passou do tamanho aceito pelo Cropilot. O próximo vai só com os aparelhos que o Cropilot usa.** Não
  precisa fazer nada. Se a mensagem voltar, avise a equipe do Cropilot.
- **Não foi possível ler os nomes dos aparelhos no Home Assistant. O relatório segue com os nomes das entidades.** Não
  precisa fazer nada: o conector tenta de novo sozinho. Se a mensagem se repetir, avise a equipe do Cropilot.
- Outras linhas trazem números e códigos para a equipe do Cropilot. Se uma delas se repetir, mande a aba Registro para
  a equipe: ela nunca mostra o código nem o pareamento.
