# Livre Bot Ponte
- Bot de ponte entre Matrix, Discord e Telegram
- Relay bot between Matrix, Discord and Telegram

- OBS: A sala não pode estar com a criptografia ativada na sala da matrix
- NOTE: The room cannot have encryption enabled in the matrix's room

## Pré-requisitos

- [Docker](https://docs.docker.com/get-docker/) e [Docker Compose](https://docs.docker.com/compose/install/) (recomendado) **ou** Python 3.11+.
- Contas e permissões nas plataformas:
  - **Matrix**: Uma conta de usuário (pode ser secundária) que será o bot. Ela deve ser convidada para as salas que deseja conectar.
  - **Discord**: Um bot com token e permissões para ler/enviar mensagens e mídia nos canais.
  - **Telegram**: Um bot (via @BotFather) com token e permissão para enviar mensagens nos grupos.
 
    OBS: Você não precisa ter a ponte nos 3 serviços ao mesmo tempo.

## Configuração Passo a Passo

### 1. Criar usuários secundários (recomendado)

#### Matrix
1. Crie uma conta separada para o bot (ex: `@meubot:matrix.org`). Você pode usar qualquer cliente Matrix (Element, etc.).
2. Nas salas que deseja conectar, convide o bot para a sala. O bot precisa ser membro para ler e enviar mensagens.
   - No Element: Abra a sala → Configurações → Membros → Convidar.

#### Discord
1. Acesse o [Portal de Desenvolvedores do Discord](https://discord.com/developers/applications).
2. Crie uma nova aplicação e depois um bot.
3. Copie o **token** do bot (você precisará no `config.json`).
4. Convide o bot para seu servidor com as permissões necessárias:
   - `Send Messages`, `Read Messages`, `Attach Files`, `Read Message History`, `Use Slash Commands` (se necessário).
   - Use o gerador de URL OAuth2: escopo `bot` e as permissões acima.

#### Telegram
1. No Telegram, converse com [@BotFather](https://t.me/botfather).
2. Use `/newbot` e siga as instruções para criar um novo bot.
3. Copie o **token** fornecido.
4. Adicione o bot ao grupo que deseja conectar e promova-o a administrador (para que ele possa ver todas as mensagens e apagar mensagens, se necessário).

### 2. Execução

1. docker compose buil --no-cache
2. docker compose up -d

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
## Prerequisites

- Docker (https://docs.docker.com/get-docker/) and Docker Compose (https://docs.docker.com/compose/install/) (recommended) **or** Python 3.11+.

- Accounts and permissions on the platforms:

- Matrix: A user account (can be secondary) that will be the bot. It must be invited to the rooms you want to connect to.

- Discord: A bot with a token and permissions to read/send messages and media in channels.

- Telegram: A bot (via @BotFather) with a token and permission to send messages in groups.

NOTE: You don't need to have the bridge on all 3 services at the same time.

## Step-by-Step Setup

### 1. Create secondary users (recommended)

#### Matrix
1. Create a separate account for the bot (e.g., `@mybot:matrix.org`). You can use any Matrix client (Element, etc.).

2. In the rooms you want to connect to, invite the bot to the room. The bot needs to be a member to read and send messages.

- In Element: Open the room → Settings → Members → Invite.

#### Discord
1. Access the [Discord Developer Portal](https://discord.com/developers/applications).

2. Create a new application and then a bot.

3. Copy the bot's **token** (you will need it in `config.json`).

4. Invite the bot to your server with the necessary permissions:

- `Send Messages`, `Read Messages`, `Attach Files`, `Read Message History`, `Use Slash Commands` (if necessary).

- Use the OAuth2 URL generator: scope `bot` and the permissions above.

#### Telegram
1. On Telegram, chat with [@BotFather](https://t.me/botfather).

2. Use `/newbot` and follow the instructions to create a new bot.

3. Copy the provided **token**.

4. Add the bot to the group you want to connect to and promote it to administrator (so it can see all messages and delete messages if necessary).

### 2. Execution

1. docker compose build --no-cache
2. docker compose up -d
