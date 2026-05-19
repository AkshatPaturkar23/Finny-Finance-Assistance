import os
import json
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
import fitz  # PyMuPDF
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from dotenv import load_dotenv
import database
import shutil
import re

load_dotenv()

app = FastAPI(title="SME Finance Assistant")
app.mount("/static", StaticFiles(directory="static"), name="static")
database.init_db()

# --- System Configuration ---
SYSTEM_PROMPT = """You are Finny, the SME Finance Assistant — a professional, sharp, and highly capable AI for Small and Medium Enterprises.

CORE RULES:
- Be direct and concise. Never say "As an AI..." or use filler phrases.
- STRICT RAG RULE: You must ONLY answer using the information found in the provided context.
- If the answer cannot be found in the provided context, you MUST say "I cannot find this information in your uploaded documents." Do NOT hallucinate or guess.
- ONLY use the provided context if it is directly relevant to the user's query. 
- If the user is just saying hello or greeting you, simply greet them back politely. Do NOT output any document data or tables.
- When listing invoices, documents, or data (if asked), ALWAYS use a clean Markdown table.
- When asked to extract or summarize data, return a structured Markdown table, NOT JSON code.
- Use **bold** for key values (totals, vendor names, dates). Use bullet points for lists.
- STOP GENERATING after you have answered the specific question. Do not volunteer extra summaries or tables unless explicitly asked.
"""

# Initialize Embeddings (using the free huggingface model option provided in prompt)
embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

# Initialize ChromaDB Vector Store
vectorstore = Chroma(embedding_function=embeddings, persist_directory="./chroma_db")

# Initialize LLM - Ollama: 100% Local, No API Keys, No Limits
# We use temperature=0.0 for maximum accuracy and zero hallucination
llm = ChatOllama(model="llama3.2:1b", base_url="http://localhost:11434", temperature=0.0)

# In-memory chat history (for demo purposes)
chat_histories = {}

# O(1) Global set for Intent Routing (reduces hard-coded data inside routes)
GREETINGS_SET = {"hi", "hello", "hey", "greetings", "good morning", "good afternoon", "hi finny", "hello finny"}

# --- Routes ---

@app.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    temp_path = f"temp_{file.filename}"
    try:
        # Save temp file
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # 1. Extract Full Text for Metadata
        doc = fitz.open(temp_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
        
        # 2. Extract Metadata using LLM for Auto-Filing & Dashboard
        metadata_prompt = f"""Extract the following details from this invoice text:
        - Vendor Name
        - Invoice Date (YYYY-MM-DD)
        - Total Amount (numeric only)
        - Category (one of: Utilities, Cloud, Office, Travel, Other)
        
        Return ONLY a JSON object like:
        {{"vendor": "name", "date": "2024-01-01", "total": 123.45, "category": "Office"}}
        
        TEXT: {full_text[:2000]}"""
        
        meta_res = llm.invoke(metadata_prompt)
        meta_data = {"vendor": "Unknown", "date": "Unknown", "total": 0.0, "category": "Other"}
        try:
            json_match = re.search(r'\{.*\}', meta_res.content, re.DOTALL)
            if json_match:
                meta_data.update(json.loads(json_match.group()))
        except:
            pass
            
        # 3. Auto-Filing: Rename file
        clean_vendor = re.sub(r'[^a-zA-Z0-9]', '_', meta_data['vendor'])
        new_filename = f"{meta_data['date']}_{clean_vendor}_{file.filename}"
        final_path = os.path.join("uploads", new_filename)
        os.rename(temp_path, final_path)
        
        # 4. Save to Database
        database.save_document(
            file.filename, new_filename, 
            meta_data['vendor'], meta_data['date'], 
            meta_data['total'], meta_data['category']
        )
        
        # 5. Process for RAG
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        chunks = text_splitter.split_text(full_text)
        if chunks:
            vectorstore.add_texts(chunks)
        
        return {
            "message": f"Auto-filed as: {new_filename}",
            "vendor": meta_data['vendor'],
            "total": meta_data['total']
        }
    except Exception as e:
        if os.path.exists(temp_path): os.remove(temp_path)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard-data")
async def get_dashboard():
    return database.get_dashboard_data()

class ProfileData(BaseModel):
    user_name: str
    company_name: str
    company_description: str

@app.get("/api/profile")
async def get_profile_api():
    return database.get_profile()

@app.post("/api/profile")
async def save_profile_api(data: ProfileData):
    database.save_profile(data.user_name, data.company_name, data.company_description)
    return {"status": "success"}

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    """Conversational chat endpoint with RAG context and memory. Returns a stream."""
    try:
        print(f"DEBUG: Processing message: {request.message}")
        
        # --- Intent Routing for Small Models ---
        # 1B models often get confused by RAG context on simple greetings. 
        # We intercept basic hellos to provide a snappy, accurate response.
        msg_lower = request.message.strip().lower()
        # Remove punctuation for matching
        msg_clean = re.sub(r'[^a-z0-9\s]', '', msg_lower).strip()
        
        # O(1) lookup against global set reduces time complexity
        if msg_clean in GREETINGS_SET:
            async def stream_greeting():
                reply = "Hello! I'm Finny. How can I help you analyze your finances today?"
                # Yield character by character to simulate typing
                for char in reply:
                    import asyncio
                    await asyncio.sleep(0.01)
                    yield char
                database.save_chat_message(request.session_id, "human", request.message)
                database.save_chat_message(request.session_id, "ai", reply)
            return StreamingResponse(stream_greeting(), media_type="text/plain")
            
        # --- Pre-Processed Suggestion Chips (Instant SQL Analytics without LLM latency) ---
        if msg_lower == 'give me a summary of q1 2026 spending':
            async def stream_q1():
                import sqlite3, asyncio
                conn = sqlite3.connect("business_assistant.db")
                c = conn.cursor()
                c.execute("SELECT SUM(total) FROM documents WHERE invoice_date LIKE '2026-01-%' OR invoice_date LIKE '2026-02-%' OR invoice_date LIKE '2026-03-%'")
                total = c.fetchone()[0] or 0.0
                conn.close()
                reply = f"**Q1 2026 Spending Summary:**\n\nYour total recorded spend for the first quarter of 2026 is **${total:,.2f}**."
                for char in reply:
                    await asyncio.sleep(0.005)
                    yield char
                database.save_chat_message(request.session_id, "human", request.message)
                database.save_chat_message(request.session_id, "ai", reply)
            return StreamingResponse(stream_q1(), media_type="text/plain")
            
        elif msg_lower == 'list my top 3 vendors by total spend':
            async def stream_vendors():
                import sqlite3, asyncio
                conn = sqlite3.connect("business_assistant.db")
                c = conn.cursor()
                c.execute("SELECT vendor, SUM(total) FROM documents GROUP BY vendor ORDER BY SUM(total) DESC LIMIT 3")
                vendors = c.fetchall()
                conn.close()
                reply = "**Top 3 Vendors by Spend:**\n\n| Rank | Vendor | Total Spend |\n|---|---|---|\n"
                for i, v in enumerate(vendors):
                    reply += f"| {i+1} | {v[0]} | ${v[1]:,.2f} |\n"
                if not vendors: reply = "No vendor data available."
                for char in reply:
                    await asyncio.sleep(0.005)
                    yield char
                database.save_chat_message(request.session_id, "human", request.message)
                database.save_chat_message(request.session_id, "ai", reply)
            return StreamingResponse(stream_vendors(), media_type="text/plain")
            
        elif msg_lower == 'are there any duplicate invoices?':
            async def stream_dupes():
                import sqlite3, asyncio
                conn = sqlite3.connect("business_assistant.db")
                c = conn.cursor()
                c.execute("SELECT vendor, total, COUNT(*) FROM documents GROUP BY vendor, total HAVING COUNT(*) > 1")
                dupes = c.fetchall()
                conn.close()
                if dupes:
                    reply = "⚠️ **Potential Duplicates Found:**\n\nI found multiple invoices with the exact same vendor and amount:\n"
                    for d in dupes:
                        reply += f"- **{d[0]}**: {d[2]} invoices for **${d[1]:,.2f}**\n"
                else:
                    reply = "✅ **Clear!** I scanned your database and found no duplicate invoices for the same vendor and amount."
                for char in reply:
                    await asyncio.sleep(0.005)
                    yield char
                database.save_chat_message(request.session_id, "human", request.message)
                database.save_chat_message(request.session_id, "ai", reply)
            return StreamingResponse(stream_dupes(), media_type="text/plain")
            
        elif msg_lower == 'summarize all software subscriptions':
            async def stream_software():
                import sqlite3, asyncio
                conn = sqlite3.connect("business_assistant.db")
                c = conn.cursor()
                c.execute("SELECT vendor, SUM(total) FROM documents WHERE vendor LIKE '%Cloud%' OR vendor LIKE '%SaaS%' OR vendor LIKE '%Tech%' GROUP BY vendor")
                softs = c.fetchall()
                conn.close()
                if softs:
                    reply = "**Software & Tech Subscriptions:**\n\n| Vendor | Total Spend |\n|---|---|\n"
                    for s in softs:
                        reply += f"| {s[0]} | ${s[1]:,.2f} |\n"
                else:
                    reply = "I don't see any obvious software or cloud subscriptions in your records yet."
                for char in reply:
                    await asyncio.sleep(0.005)
                    yield char
                database.save_chat_message(request.session_id, "human", request.message)
                database.save_chat_message(request.session_id, "ai", reply)
            return StreamingResponse(stream_software(), media_type="text/plain")

        docs = vectorstore.similarity_search(request.message, k=4) # Increased to 4 to give the model more accurate context
        context = "\n\n".join([doc.page_content for doc in docs])
        
        db_history = database.get_chat_history(request.session_id)
        history = []
        for role, content in db_history:
            if role == "human": history.append(HumanMessage(content=content))
            else: history.append(AIMessage(content=content))
            
        profile = database.get_profile()
        user_name = profile.get('user_name') or 'User'
        company = profile.get('company_name') or 'their company'
        desc = profile.get('company_description') or 'business'
        
        profile_context = f"\n\nUSER PROFILE:\nYou are speaking to {user_name}, who works at {company} ({desc}). Personalize your responses to them using this information."
            
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT + profile_context + "\n\nContext to help answer:\n{context}"),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{message}")
        ])
        
        chain = prompt | llm
        
        async def generate_response():
            full_response = ""
            try:
                # Stream the response chunk by chunk
                async for chunk in chain.astream({
                    "context": context,
                    "message": request.message,
                    "history": history
                }):
                    content_text = chunk.content if hasattr(chunk, 'content') else str(chunk)
                    full_response += content_text
                    yield content_text
                
                # Save to database after completion
                database.save_chat_message(request.session_id, "human", request.message)
                database.save_chat_message(request.session_id, "ai", full_response)
                
            except Exception as e:
                yield f"\n\nError generating response: {str(e)}"
                
        return StreamingResponse(generate_response(), media_type="text/plain")
        
    except Exception as e:
        error_msg = str(e)
        if "RESOURCE_EXHAUSTED" in error_msg or "429" in error_msg:
            return StreamingResponse(iter(["I've reached my API rate limit. Please try again."]), media_type="text/plain")
        if "restricted" in error_msg.lower():
            return StreamingResponse(iter(["Your Groq API key is restricted."]), media_type="text/plain")
        if "connection" in error_msg.lower() or "11434" in error_msg:
            return StreamingResponse(iter(["I couldn't connect to Ollama. Make sure it's running."]), media_type="text/plain")
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/extract")
async def extract_data(file: UploadFile = File(...)):
    """For invoice/data parsing using chaining logic."""
    if not file.filename.endswith(('.pdf')):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        
    try:
        content = await file.read()
        pdf_document = fitz.open(stream=content, filetype="pdf")
        text = ""
        for page in pdf_document:
            text += page.get_text()
            
        # Agentic Approach: OCR Text -> JSON Extractor -> Math Validator
        # Here we simplify the chain into a strong single prompt acting as an extractor
        prompt = PromptTemplate(
            input_variables=["text", "system_prompt"],
            template="""{system_prompt}
            
            Extract the following invoice data from the OCR Text below.
            Output ONLY a JSON block followed by a brief human-readable summary.
            
            Required JSON keys:
            - vendor (string)
            - invoice_no (string)
            - total_amount (float)
            - currency (string)
            - confidence_score (float 0.0 to 1.0)
            
            OCR Text:
            {text}
            """
        )
        
        chain = prompt | llm
        response = chain.invoke({"text": text, "system_prompt": SYSTEM_PROMPT})
        
        return {"filename": file.filename, "extraction": response.content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
