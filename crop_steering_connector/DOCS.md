# Conector do Crop Steering

O Conector do Crop Steering liga este Home Assistant ao Cropilot. Com ele, o Cropilot lê os sensores de temperatura e
umidade e comanda o ar-condicionado, o desumidificador e as tomadas que você liberar no Cropilot.

## O que ele faz e o que não faz

- Ele só abre conexões de saída, com HTTPS, até o Cropilot. Ele não abre nenhuma porta no computador nem na sua rede,
  e o Cropilot nunca entra na sua rede.
- Ele não tem página: não aparece na barra lateral e não tem tela para abrir. O que ele faz aparece na aba Registro.
- Ele manda ao Cropilot só os sensores de temperatura, umidade, potência e energia, os aparelhos de clima (como o
  ar-condicionado), os umidificadores, os desumidificadores e as tomadas deste Home Assistant, com o nome de cada
  aparelho. Fechaduras, alarmes, câmeras e o resto ficam de fora.
- Ele só comanda o que você liberar no Cropilot. O ar-condicionado, o desumidificador e as tomadas que o Home
  Assistant mostra no aparelho deles seguem a proteção de 10 minutos do Cropilot.
- No desumidificador que aceita umidade programada, o Cropilot muda essa umidade em vez de ligar e desligar o aparelho.
- O exaustor entra numa tomada só para conferir os pulsos que você programa no timer de ciclo dela, no app Smart Life:
  o Cropilot nunca liga nem desliga o exaustor sozinho. A bomba de rega entra só pela rega automática, com o pulso
  gravado na tomada: veja “Bomba de rega”. O Cropilot não comanda umidificador: com a umidade baixa, ele mostra dicas
  para subir a umidade à mão.
- O Cropilot não guarda a senha do Smart Life nem a de nenhuma conta dos seus aparelhos: as contas ficam
  só neste Home Assistant.
- Ele não precisa de nenhum serviço pago.
- Use um Home Assistant só do cultivo: uma tomada liberada pode ser ligada e desligada pelo Cropilot.
- Para o Cropilot ler e comandar os aparelhos, o computador precisa ficar ligado e com internet, e o conector,
  iniciado.

## Instalar

1. Clique em “Instalar”. A instalação leva alguns minutos.
2. Na aba Informações, deixe ligados “Iniciar na inicialização” e “Watchdog”. Assim o conector inicia junto com o Home
   Assistant e volta sozinho se parar por um erro.

## Parear com o Cropilot

1. No Cropilot, em Perfil › Conexões, clique em “Gerar código”. O código aparece uma vez, vale 15 minutos e serve
   para um pareamento só.
2. Na aba Configuração, cole o código em “Código de pareamento”.
3. Clique em “Salvar”.
4. Na aba Informações, clique em “Iniciar”. Se o conector já estava iniciado, clique em “Reiniciar”.
5. Na aba Registro, confira a linha **Pareado com o Cropilot.**

Depois do pareamento, o código não é mais usado e pode ficar no campo.

## Tomadas

A tomada que o Home Assistant mostra no aparelho de um ar-condicionado ou de um desumidificador liga e desliga com a
proteção de 10 minutos e não pode ser a bomba de rega, a luz nem o exaustor do espaço. Um desumidificador simples
ligado numa tomada inteligente entra pela tomada: escolhida como o desumidificador do espaço, ela liga e desliga com a
proteção de 10 minutos enquanto estiver nessa função. Não escolha essa tomada como luz nem como bomba de rega. Qualquer
outra tomada liberada liga e desliga como uma tomada simples, sem essa proteção. Um ar-condicionado ligado numa tomada
inteligente aparece como uma tomada simples: não libere essa tomada no Cropilot.

## Bomba de rega

O Cropilot liga a tomada da bomba nos horários da grade. Quem desliga é a própria tomada, no fim do pulso que você
grava no app Smart Life. A bomba não é usada à mão: o conector confere cada disparo e tenta desligar a tomada se a
bomba ficar ligada além do pulso ou ligar fora de um disparo, e o Cropilot para a rega automática e avisa você. Ao
iniciar, o conector tenta desligar a bomba que encontrar ligada fora de um disparo. A conferência continua sem contato
com o Cropilot, mas o desligamento passa pela nuvem do Smart Life: sem internet, só o pulso gravado na tomada desliga a
bomba.

## Segurança

- A página do conector mostra a nota de segurança 8, numa escala de 1 a 8. Antes da instalação, a loja mostra 7,
  porque o perfil de proteção do conector só conta depois que ele é instalado.
- Não passe o código de pareamento para ninguém: quem tiver o código liga outro Home Assistant à sua conta.
- O pareamento fica guardado só neste Home Assistant, e a aba Registro nunca mostra o código nem o pareamento.
- As cópias de segurança do Home Assistant não levam o pareamento. Depois de restaurar uma cópia, gere um código novo
  em Perfil › Conexões, no Cropilot, cole em “Código de pareamento”, clique em “Salvar” e depois em “Reiniciar”.
- Se este computador ou este Home Assistant passar para outra pessoa, desconecte o Home Assistant em
  Perfil › Conexões, no Cropilot.

## Mensagens da aba Registro

- **Aguardando o Home Assistant terminar de iniciar.** O Home Assistant ainda está iniciando. Não precisa fazer nada:
  o conector continua sozinho.
- **Pareado com o Cropilot.** Tudo certo: o conector passa a mandar os aparelhos ao Cropilot e a receber os comandos.
- **Falta o código de pareamento. Gere o código em Perfil › Conexões, no Cropilot, e cole em “Código de pareamento”,
  na aba Configuração.** Depois de colar, clique em “Salvar” e depois em “Reiniciar”.
- **Código de pareamento vencido ou já usado. Gere um código novo em Perfil › Conexões, no Cropilot, e cole em
  “Código de pareamento”.** Cole o código novo no lugar do antigo, clique em “Salvar” e depois em “Reiniciar”.
- **Pareamento revogado. Gere um código novo em Perfil › Conexões, no Cropilot, e cole em “Código de pareamento”.**
  O pareamento foi desfeito no Cropilot, e as leituras e os comandos pararam. Com o código novo, clique em “Salvar” e
  depois em “Reiniciar”.
- **A conta no Cropilot não está liberada para o conector. Fale com a equipe do Cropilot.** O conector tenta de novo
  sozinho, de tempos em tempos.
- **O relatório passou do tamanho aceito pelo Cropilot. O próximo vai só com os aparelhos liberados.** Não precisa
  fazer nada. Se a mensagem voltar, avise a equipe do Cropilot.
- **Não foi possível ler os nomes dos aparelhos no Home Assistant. O relatório segue com os nomes das entidades.** Não
  precisa fazer nada: o conector tenta de novo sozinho. Se a mensagem se repetir, avise a equipe do Cropilot.
- Outras linhas trazem números e códigos para a equipe do Cropilot. Se uma delas se repetir, mande a aba Registro para
  a equipe: ela nunca mostra o código nem o pareamento.
