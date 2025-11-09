import os
import google.generativeai as genai
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2
import traceback
import json
import requests 

# A v3.1 corrige o bug do 'system_instruction'
print("ℹ️  Iniciando a API do [SUA_GRÁFICA BOT] (v3.1 - Correção de Erro)...")
load_dotenv()

app = Flask(__name__)
CORS(app)

try:
    DATABASE_URL = os.environ.get("DATABASE_URL")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    SALES_WEBHOOK_URL = os.environ.get("SALES_WEBHOOK_URL") 
    N8N_SECRET_KEY = os.environ.get("N8N_SECRET_KEY", "sua-chave-secreta-padrao") 

    if not DATABASE_URL or not GEMINI_API_KEY:
        print("❌ ERRO CRÍTICO: DATABASE_URL ou GEMINI_API_KEY não encontradas.")
    if not SALES_WEBHOOK_URL:
        print("⚠️ AVISO: SALES_WEBHOOK_URL não configurada.")
    if not N8N_SECRET_KEY:
        print("⚠️ AVISO: N8N_SECRET_KEY não configurada. A atualização de status pelo N8N pode falhar.")
        
    genai.configure(api_key=GEMINI_API_KEY)
    # Modelo global para endpoints SEM instrução de sistema dinâmica
    model = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025') 
    print("✅  [Gemini] Modelo ('gemini-2.5-flash-preview-09-2025') inicializado.")

except Exception as e:
    model = None
    print(f"❌ Erro ao carregar chaves ou configurar Gemini: {e}")
    traceback.print_exc()

# --- 3. [HELPER] SQL para Criar/Atualizar Tabelas ---
CREATE_ELO_LEADS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS elo_leads (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    nome VARCHAR(255),
    email VARCHAR(255), -- UNIQUE FOI REMOVIDO PARA TESTES
    empresa_ramo VARCHAR(255),
    cargo VARCHAR(255),
    cnpj_fornecido VARCHAR(50),
    status_lead VARCHAR(50) DEFAULT 'Frio',
    recomendacoes_ia JSONB, 
    historico_chat JSONB,
    email_enviado BOOLEAN DEFAULT false,
    ja_e_cliente VARCHAR(50) 
);
"""
CREATE_ELO_ORCAR_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS elo_orçar (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    produto_desejado TEXT,
    quantidade_estimada VARCHAR(100),
    prazo_entrega VARCHAR(255),
    tipo_de_gravacao VARCHAR(255),
    cidade_entrega VARCHAR(255),
    estado_entrega VARCHAR(100),
    lead_id INTEGER REFERENCES elo_leads(id)
);
"""
ADD_NEW_COLUMNS_SQL = """
ALTER TABLE elo_leads 
ADD COLUMN IF NOT EXISTS whatsapp VARCHAR(50),
ADD COLUMN IF NOT EXISTS isca TEXT,
ADD COLUMN IF NOT EXISTS status VARCHAR(100) DEFAULT 'Novo';
"""
# (SQL para remover a trava de email)
DROP_UNIQUE_CONSTRAINT_SQL = """
ALTER TABLE elo_leads 
DROP CONSTRAINT IF EXISTS elo_leads_email_key;
"""

# --- 4. [HELPER] Função de Setup do Banco (ATUALIZADA) ---
def setup_database():
    """Conecta ao banco e garante que TODAS as tabelas e colunas existam."""
    conn = None
    try:
        if not DATABASE_URL:
            print("⚠️ AVISO [DB]: DATABASE_URL não configurada.")
            return

        print("ℹ️  [DB] Conectando ao PostgreSQL para verificar tabelas...")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        print("ℹ️  [DB] Verificando 'elo_leads' (base)...")
        cur.execute(CREATE_ELO_LEADS_TABLE_SQL)
        
        print("ℹ️  [DB] Verificando 'elo_orçar'...")
        cur.execute(CREATE_ELO_ORCAR_TABLE_SQL)
        
        print("ℹ️  [DB] Verificando colunas 'whatsapp', 'isca', 'status' em 'elo_leads'...")
        cur.execute(ADD_NEW_COLUMNS_SQL)
        
        # (Adicionado - Garante que a trava de email saiu para os testes)
        print("ℹ️  [DB] Removendo trava 'UNIQUE' do email para testes...")
        cur.execute(DROP_UNIQUE_CONSTRAINT_SQL)

        conn.commit()
        cur.close()
        print("✅  [DB] Tabelas e colunas verificadas/criadas com sucesso.")
        
    except psycopg2.Error as e:
        print(f"❌ ERRO [DB] ao configurar as tabelas: {e}")
        if conn: conn.rollback()
    except Exception as e:
        print(f"❌ ERRO Inesperado [DB] em setup_database: {e}")
    finally:
        if conn: conn.close()

# --- 5. Endpoints da API ---

@app.route('/')
def index():
    return jsonify({"message": "API [SUA_GRÁFICA BOT] (v3.1 - Funil N8N) está rodando!"})

@app.route('/api/save-lead', methods=['POST'])
def save_lead():
    """(Endpoint de finalização - usado para o CNPJ)"""
    print("\n--- Recebido trigger para /api/save-lead (Finalização/CNPJ) ---")
    data = request.get_json()
    
    lead_id = data.get('lead_id')
    cargo = data.get('cargo')
    cnpj = data.get('cnpj_fornecido')
    historico = data.get('historico_chat')
    
    if not data or not lead_id:
        return jsonify({"error": "lead_id é obrigatório."}), 400

    status = 'Frio'
    cargos_quentes = ['marketing', 'comprador', 'diretor', 'compras', 'ceo', 'agencia', 'mkt']
    if cargo and cnpj and any(c in cargo.lower() for c in cargos_quentes) and cnpj.lower() not in ['nao', 'não', 'n', '']:
        status = 'Quente'

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        historico_json = json.dumps(historico)
        
        print(f"ℹ️  [DB] Executando UPDATE para Lead ID: {lead_id} (CNPJ/Final)")
        sql = """
        UPDATE elo_leads SET 
            cnpj_fornecido = COALESCE(%s, cnpj_fornecido),
            status_lead = %s,
            historico_chat = %s
        WHERE id = %s
        RETURNING id;
        """
        cur.execute(sql, (cnpj, status, historico_json, lead_id))
        final_lead_id = cur.fetchone()[0]
        
        conn.commit()
        print(f"✅  [DB] Lead finalizado com ID: {final_lead_id} (Status: {status})")
        return jsonify({"success": True, "lead_id": final_lead_id, "status": status}), 201
        
    except Exception as e:
        print(f"❌ ERRO [DB] ao salvar o lead (final): {e}")
        traceback.print_exc()
        if conn: conn.rollback()
        return jsonify({"error": f"Erro ao salvar o lead: {e}"}), 500
    finally:
        if conn:
            cur.close()
            conn.close()

# --- (ENDPOINT DE CHAT CORRIGIDO) ---
@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Recebe o histórico da conversa e os dados do lead,
    retorna a resposta da IA e os dados extraídos.
    """
    print("\n--- Recebido trigger para /api/chat ---")
    if not model: # 'model' aqui se refere ao 'model' global
        return jsonify({"error": "Serviço de IA não está disponível."}), 503

    data = request.get_json()
    history = data.get('conversationHistory', [])
    lead_data = data.get('leadData', {})
    lead_id = data.get('leadId')

    # 1. Define o "cérebro" da IA (System Prompt ATUALIZADO)
    system_prompt = f"""
    Você é o [SUA_GRÁFICA BOT], um assistente virtual amigável e proativo da [SUA_GRÁFICA BOT].
    Seu objetivo principal é coletar os seguintes 6 dados do cliente: "nome", "empresa_ramo", "cargo", "email", "ja_e_cliente", "whatsapp".
    
    Estes são os dados que já temos: {lead_data}
    
    REGRAS DA CONVERSA:
    1.  Converse naturalmente. Peça o próximo dado FALTANDO da lista (nome -> empresa_ramo -> cargo -> email -> ja_e_cliente -> whatsapp).
    2.  NÃO peça por dados que já estão preenchidos na lista {lead_data}.
    3.  Após coletar o "email", a próxima pergunta DEVE ser "Você já é cliente da [SUA_GRÁFICA BOT]? (Sim ou Não)".
    4.  Após coletar o "ja_e_cliente", a próxima pergunta DEVE ser "Ótimo! E qual o seu WhatsApp com DDD? (para agilizar o contato)".
    5.  Se o usuário fornecer vários dados de uma vez, capture todos.
    
    FORMATO DA RESPOSTA (JSON obrigatório):
    {{
        "botResponse": "O texto da sua resposta para o usuário.",
        "extractedData": {{
            "nome": "[O nome extraído ESTA RODADA]",
            "empresa_ramo": "[O ramo extraído ESTA RODADA]",
            "cargo": "[O cargo extraído ESTA RODADA]",
            "email": "[O email extraído ESTA RODADA]",
            "ja_e_cliente": "[O 'Sim' ou 'Não' extraído ESTA RODADA]",
            "whatsapp": "[O whatsapp extraído ESTA RODADA]"
        }}
    }}
    """
    
    gemini_history = []
    for message in history:
        role = 'user' if message['role'] == 'user' else 'model'
        gemini_history.append({'role': role, 'parts': [{'text': message['text']}]})

    try:
        print(f"ℹ️  [Gemini] Chamando IA com dados: {lead_data}")

        # --- [A CORREÇÃO ESTÁ AQUI] ---
        # 1. Cria um novo modelo LOCAL com a instrução de sistema dinâmica
        chat_model = genai.GenerativeModel(
            'gemini-2.5-flash-preview-09-2025',
            system_instruction=system_prompt
        )
        
        # 2. Chama generate_content NESSE modelo (sem o system_instruction como kwarg)
        response = chat_model.generate_content(
            gemini_history, # Passa o histórico
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                response_mime_type="application/json"
            ),
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE', 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
            # 'system_instruction' foi REMOVIDO daqui
        )
        # --- [FIM DA CORREÇÃO] ---
        
        gemini_response = json.loads(response.text)
        print(f"✅  [Gemini] Resposta da IA: {gemini_response}")
        
        bot_response_text = gemini_response.get('botResponse', 'Desculpe, não entendi. Pode repetir?')
        extracted_data = gemini_response.get('extractedData', {})
        
        is_complete = False
        new_lead_data = lead_data.copy()
        
        for key, value in extracted_data.items():
            if key in ['nome', 'email', 'empresa_ramo', 'cargo', 'ja_e_cliente', 'whatsapp'] and value:
                new_lead_data[key] = value
                
        current_history_json = json.dumps(history + [
            {'role': 'bot', 'text': bot_response_text, 'time': 'now'}
        ])
        
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            
            if lead_id:
                print(f"ℹ️  [DB-Chat] Executando UPDATE para Lead ID: {lead_id}")
                sql = """
                UPDATE elo_leads SET 
                    nome = COALESCE(%s, nome),
                    email = COALESCE(%s, email),
                    empresa_ramo = COALESCE(%s, empresa_ramo),
                    cargo = COALESCE(%s, cargo),
                    ja_e_cliente = COALESCE(%s, ja_e_cliente),
                    whatsapp = COALESCE(%s, whatsapp), 
                    historico_chat = %s
                WHERE id = %s
                RETURNING id;
                """
                cur.execute(sql, (
                    new_lead_data.get('nome'), new_lead_data.get('email'), 
                    new_lead_data.get('empresa_ramo'), new_lead_data.get('cargo'),
                    new_lead_data.get('ja_e_cliente'), new_lead_data.get('whatsapp'),
                    current_history_json, lead_id
                ))
                final_lead_id = cur.fetchone()[0]

            else:
                # Como não temos mais a trava de email, a lógica de INSERT é mais simples
                print("ℹ️  [DB-Chat] Executando INSERT (sem trava de email).")
                sql = """
                INSERT INTO elo_leads (nome, email, empresa_ramo, cargo, ja_e_cliente, whatsapp, historico_chat, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'Coletando')
                RETURNING id;
                """
                cur.execute(sql, (
                    new_lead_data.get('nome'), new_lead_data.get('email'),
                    new_lead_data.get('empresa_ramo'), new_lead_data.get('cargo'),
                    new_lead_data.get('ja_e_cliente'), new_lead_data.get('whatsapp'),
                    current_history_json
                ))
                final_lead_id = cur.fetchone()[0]

            conn.commit()
            lead_id = final_lead_id 
            print(f"✅  [DB-Chat] Lead salvo/atualizado com ID: {lead_id}")

        except Exception as e_db:
            print(f"❌ ERRO [DB-Chat] ao salvar o lead: {e_db}")
            traceback.print_exc()
            if conn: conn.rollback()
        finally:
            if conn:
                cur.close()
                conn.close()
        
        if (new_lead_data.get('nome') and 
            new_lead_data.get('empresa_ramo') and 
            new_lead_data.get('cargo') and 
            new_lead_data.get('email') and
            new_lead_data.get('ja_e_cliente') and
            new_lead_data.get('whatsapp')):
            
            is_complete = True
            print("✅  [IA] Coleta de dados (6/6) completa!")

        return jsonify({
            "botResponse": bot_response_text,
            "leadData": new_lead_data,
            "leadId": lead_id,
            "isComplete": is_complete
        })

    except Exception as e_gen:
        print(f"❌ ERRO [Gemini] ao gerar resposta do chat: {e_gen}")
        traceback.print_exc()
        return jsonify({"error": "Erro ao processar a resposta da IA."}), 500

@app.route('/api/generate-recommendations', methods=['POST'])
def generate_recommendations():
    """
    Gera a "Isca" de MKT, salva na coluna 'isca' 
    e atualiza o 'status' para o N8N.
    """
    print("\n--- Recebido trigger para /api/generate-recommendations ---")
    if not model:
        return jsonify({"error": "Serviço de IA não está disponível."}), 503
        
    data = request.get_json()
    lead_id = data.get('lead_id')
    ramo = data.get('ramo')

    if not lead_id or not ramo:
        return jsonify({"error": "ID do Lead e Ramo são obrigatórios."}), 400

    try:
        prompt = f"""
        Você é um especialista de marketing sênior da [SUA_GRÁFICA BOT].
        Um cliente do ramo de "{ramo}" pediu 5 ideias de brindes. 
        
        Gere uma lista com 5 ideias de brindes que se encaixam perfeitamente nesse nicho. 
        Para cada brinde, dê o NOME DO BRINDE e uma frase curta (1 linha) explicando POR QUE ele é bom para esse ramo.
        
        Separe as ideias por faixas de preço:
        
        **Brindes de Alto Impacto (Premium):**
        1. [Nome do Brinde 1]: [Explicação de 1 linha]
        2. [Nome do Brinde 2]: [Explicação de 1 linha]
        
        **Brindes do Dia-a-Dia (Custo-Benefício):**
        3. [Nome do Brinde 3]: [Explicação de 1 linha]
        4. [Nome do Brinde 4]: [Explicação de 1 linha]
        
        **Brindes de Grande Volume (Econômico):**
        5. [Nome do Brinde 5]: [Explicação de 1 linha]
        """
        
        print(f"ℹ️  [Gemini] Gerando recomendações para o ramo: {ramo}")
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(temperature=0.5),
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE', 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'}
        )
        
        recomendacoes_texto = response.text
        print(f"✅  [Gemini] Recomendações (Isca) geradas.")

        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            
            cur.execute("""
                UPDATE elo_leads 
                SET 
                    isca = %s, 
                    status = %s,
                    email_enviado = %s
                WHERE id = %s
            """, (
                recomendacoes_texto,
                'Aguardando Envio N8N',
                False,
                lead_id
            ))
            
            conn.commit()
            print(f"✅  [DB] Isca salva e status atualizado para 'Aguardando Envio N8N' no Lead ID: {lead_id}")

        except Exception as e_db:
            print(f"❌ ERRO [DB] ao ATUALIZAR lead com a isca: {e_db}")
            if conn: conn.rollback()
        finally:
            if conn:
                cur.close()
                conn.close()

        return jsonify({"success": True, "message": "Isca gerada e salva no DB."})

    except Exception as e_gen:
        print(f"❌ ERRO [Gemini] ao gerar recomendações: {e_gen}")
        traceback.print_exc()
        return jsonify({"error": "Erro ao gerar as recomendações."}), 500

@app.route('/api/save-quote', methods=['POST'])
def save_quote():
    """
    Recebe os dados do orçamento, salva na tabela 'elo_orçar'
    e dispara o Webhook de VENDAS (N8N) para o orçamentista.
    """
    print("\n--- Recebido trigger para /api/save-quote (ORÇAMENTO) ---")
    
    data = request.get_json()
    lead_id = data.get('lead_id')
    
    quote_data = data.get('quote_data', {})
    produto = quote_data.get('produto_desejado')
    quantidade = quote_data.get('quantidade_estimada')
    prazo = quote_data.get('prazo_entrega')
    gravacao = quote_data.get('tipo_de_gravacao')
    cidade = quote_data.get('cidade_entrega')
    estado = quote_data.get('estado_entrega')

    if not lead_id or not produto or not quantidade:
        return jsonify({"error": "lead_id, produto e quantidade são obrigatórios."}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        print(f"ℹ️  [DB] Salvando orçamento para Lead ID: {lead_id}...")
        sql_orcar = """
        INSERT INTO elo_orçar 
            (lead_id, produto_desejado, quantidade_estimada, prazo_entrega, tipo_de_gravacao, cidade_entrega, estado_entrega)
        VALUES 
            (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """
        cur.execute(sql_orcar, (lead_id, produto, quantidade, prazo, gravacao, cidade, estado))
        orcamento_id = cur.fetchone()[0]
        print(f"✅  [DB] Orçamento ID: {orcamento_id} salvo com sucesso.")

        print(f"ℹ️  [DB] Buscando dados do Lead ID: {lead_id} para o webhook...")
        cur.execute("SELECT nome, email, empresa_ramo, cargo, ja_e_cliente, whatsapp FROM elo_leads WHERE id = %s", (lead_id,))
        lead_info = cur.fetchone()
        
        if not lead_info:
            print(f"❌ ERRO [Webhook]: Não foi possível encontrar o lead (ID: {lead_id}) para disparar o webhook.")
            conn.commit()
            return jsonify({"success": True, "orcamento_id": orcamento_id, "webhook_status": "erro_lead_nao_encontrado"}), 201

        if SALES_WEBHOOK_URL:
            webhook_payload = {
                "lead_id": lead_id,
                "orcamento_id": orcamento_id,
                "nome": lead_info[0],
                "email": lead_info[1],
                "empresa_ramo": lead_info[2],
                "cargo": lead_info[3],
                "ja_e_cliente": lead_info[4],
                "whatsapp": lead_info[5],
                "produto_desejado": produto,
                "quantidade_estimada": quantidade,
                "prazo_entrega": prazo,
                "tipo_de_gravacao": gravacao,
                "cidade_entrega": cidade,
                "estado_entrega": estado
            }
            
            print(f"ℹ️  [Webhook] Disparando webhook de VENDAS para: {SALES_WEBHOOK_URL}")
            try:
                requests.post(SALES_WEBHOOK_URL, json=webhook_payload, timeout=3)
                print("✅  [Webhook] Webhook de VENDAS disparado.")
            except requests.RequestException as e_req:
                print(f"❌ ERRO [Webhook] Falha ao disparar o webhook: {e_req}")
        
        else:
            print("⚠️  [Webhook] SALES_WEBHOOK_URL não configurada. Webhook não disparado.")

        conn.commit()
        return jsonify({"success": True, "orcamento_id": orcamento_id, "webhook_status": "disparado" if SALES_WEBHOOK_URL else "nao_configurado"}), 201

    except Exception as e:
        print(f"❌ ERRO [DB] ao salvar o orçamento: {e}")
        traceback.print_exc()
        if conn: conn.rollback()
        return jsonify({"error": f"Erro ao salvar o orçamento: {e}"}), 500
    finally:
        if conn:
            cur.close()
            conn.close()

@app.route('/api/update-status-n8n', methods=['POST'])
def update_status_n8n():
    """
    Endpoint SEGURO para o N8N chamar DEPOIS de enviar o e-mail da isca.
    """
    print("\n--- Recebido trigger para /api/update-status-n8n ---")
    
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {N8N_SECRET_KEY}":
        print("❌ ERRO [Auth]: Tentativa de acesso não autorizada ao /api/update-status-n8n.")
        return jsonify({"error": "Não autorizado"}), 401
        
    data = request.get_json()
    lead_id = data.get('lead_id')
    new_status = data.get('new_status')

    if not lead_id or not new_status:
        return jsonify({"error": "lead_id e new_status são obrigatórios."}), 400

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        print(f"ℹ️  [DB-N8N] Atualizando status do Lead ID: {lead_id} para '{new_status}'...")
        cur.execute("""
            UPDATE elo_leads 
            SET status = %s, email_enviado = %s
            WHERE id = %s
        """, (new_status, True, lead_id))
        
        conn.commit()
        print("✅  [DB-N8N] Status atualizado com sucesso.")
        return jsonify({"success": True, "lead_id": lead_id, "new_status": new_status}), 200

    except Exception as e:
        print(f"❌ ERRO [DB-N8N] ao atualizar o status: {e}")
        traceback.print_exc()
        if conn: conn.rollback()
        return jsonify({"error": f"Erro ao atualizar o status: {e}"}), 500
    finally:
        if conn:
            cur.close()
            conn.close()

# --- 7. Execução do App (Pronto para Render/Gunicorn) ---
if __name__ == "__main__":
    setup_database()
    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port, debug=False) # Debug=False é melhor para produção

