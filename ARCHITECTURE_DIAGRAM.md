# Architecture Diagram - PR Review Agent

This file contains Mermaid diagrams for the PR Review Agent architecture. You can:
1. **View on GitHub** — Mermaid renders automatically
2. **Export to PNG** — Use [mermaid.live](https://mermaid.live)
3. **Use in docs** — Copy-paste into any markdown file

---

## System Architecture

```mermaid
graph TD
    A["GitHub Webhook<br/>(PR Events)"] -->|HTTP| B["FastAPI Server<br/>(Webhook Receiver)"]

    B -->|Init State| C["LangGraph State Machine<br/>(Orchestration Engine)"]

    C -->|Route| D1["Fetch Node<br/>(PR Metadata)"]
    C -->|Route| D2["Validate Node<br/>(Input Check)"]
    C -->|Route| D3["Analyze Node<br/>(Diff Parse)"]
    C -->|Route| D4["Review Node<br/>(LLM Analysis)"]
    C -->|Route| D5["Publish Node<br/>(Results)"]
    C -->|Checkpoint| D6["SQLite State<br/>(Persistence)"]

    D1 -->|API Call| E1["GitHub API<br/>Client"]
    D3 -->|Semantic Search| E3["FAISS RAG<br/>(Code Context)"]
    D4 -->|LLM Request| E2["Google Gemini<br/>LLM Service"]
    D5 -->|Async Notify| E4["Slack<br/>Notifications"]

    D5 -->|Output| F["GitHub PR Comments<br/>(Review Findings)"]

    style A fill:#e8f4f8,stroke:#333,stroke-width:2px
    style B fill:#ffe8cc,stroke:#333,stroke-width:2px
    style C fill:#e8f8e8,stroke:#333,stroke-width:2px
    style D1 fill:#e8f8e8,stroke:#333,stroke-width:2px
    style D2 fill:#e8f8e8,stroke:#333,stroke-width:2px
    style D3 fill:#e8f8e8,stroke:#333,stroke-width:2px
    style D4 fill:#e8f8e8,stroke:#333,stroke-width:2px
    style D5 fill:#e8f8e8,stroke:#333,stroke-width:2px
    style D6 fill:#f0f0f0,stroke:#333,stroke-width:2px
    style E1 fill:#f0e8f8,stroke:#333,stroke-width:2px
    style E2 fill:#f0e8f8,stroke:#333,stroke-width:2px
    style E3 fill:#f0e8f8,stroke:#333,stroke-width:2px
    style E4 fill:#f0e8f8,stroke:#333,stroke-width:2px
    style F fill:#ffe8e8,stroke:#333,stroke-width:2px
```

---

## Data Flow Sequence

```mermaid
sequenceDiagram
    participant GitHub
    participant FastAPI
    participant LangGraph
    participant Services
    participant Output

    GitHub->>FastAPI: Webhook Event (PR opened/updated)
    FastAPI->>FastAPI: Verify HMAC signature
    FastAPI->>LangGraph: Create PR review state

    LangGraph->>Services: Fetch PR metadata (GitHub API)
    LangGraph->>LangGraph: Validate input (file count, diff size)
    LangGraph->>Services: Index codebase (RAG/FAISS)
    LangGraph->>LangGraph: Parse unified diff format

    LangGraph->>Services: Send code chunks to Gemini LLM
    Services->>LangGraph: Receive LLM analysis

    LangGraph->>LangGraph: Merge findings (9-dimension review)
    LangGraph->>Output: Format review comments
    LangGraph->>Output: Post to GitHub PR
    LangGraph->>Services: Send Slack notification

    Output->>GitHub: Comments appear on PR
```

---

## LangGraph Node Flow

```mermaid
graph LR
    A["fetch_pr<br/>(GitHub API)"] -->|PR data| B["validate<br/>(Constraints)"]
    B -->|Valid| C["analyze_diff<br/>(Parse Chunks)"]
    B -->|Invalid| Z1["Skip Review"]

    C -->|Chunks| D["build_rag<br/>(FAISS Index)"]
    D -->|Index ready| E["review_chunk<br/>(Loop)"]

    E -->|For each chunk| F["LLM Analysis<br/>(9 Dimensions)"]
    F -->|Findings| G["validate_findings<br/>(Merge Results)"]
    G -->|More chunks| E
    G -->|Done| H["publish_results<br/>(GitHub + Slack)"]

    H -->|Success| Z2["Review Complete"]
    H -->|Error| Z3["Error Handling"]

    style A fill:#e8f8e8
    style B fill:#e8f8e8
    style C fill:#e8f8e8
    style D fill:#e8f8e8
    style E fill:#e8f8e8
    style F fill:#e8f8e8
    style G fill:#e8f8e8
    style H fill:#e8f8e8
    style Z1 fill:#ffe8e8
    style Z2 fill:#e8f8e8
    style Z3 fill:#ffe8e8
```

---

## Deployment Architecture

```mermaid
graph TB
    A["Docker Container<br/>(Image)"]

    B["Environment Variables<br/>(Secrets)"]
    C["Python App<br/>(LangGraph + FastAPI)"]
    D["SQLite Database<br/>(Checkpoint)"]

    E["GitHub API"]
    F["Google Gemini LLM"]
    G["Slack Webhook"]
    H["GitHub Webhook<br/>(Incoming)"]

    B -->|Config| C
    A -->|Contains| C
    C -->|Reads/Writes| D
    C -->|API Calls| E
    C -->|LLM Requests| F
    C -->|Notifications| G
    H -->|Events| C

    style A fill:#ffe8cc
    style B fill:#f0f0f0
    style C fill:#e8f8e8
    style D fill:#f0f0f0
    style E fill:#f0e8f8
    style F fill:#f0e8f8
    style G fill:#f0e8f8
    style H fill:#e8f4f8
```

---

## State Machine Transitions

```mermaid
stateDiagram-v2
    [*] --> CheckConfig
    CheckConfig --> Fetch: Config valid
    CheckConfig --> Skip: Config invalid

    Fetch --> Validate: PR fetched
    Validate --> Analyze: Input valid
    Validate --> Skip: Constraints exceeded

    Analyze --> BuildRAG: Diff parsed
    BuildRAG --> ReviewLoop: Index ready

    ReviewLoop --> LLMAnalysis: Chunk available
    LLMAnalysis --> MergeFindings: Analysis complete
    MergeFindings --> ReviewLoop: More chunks
    MergeFindings --> Publish: All chunks done

    Publish --> Complete: Results posted

    Fetch --> ErrorState: API failure
    Validate --> ErrorState: Parse failure
    ReviewLoop --> ErrorState: LLM timeout
    Publish --> ErrorState: GitHub error

    ErrorState --> Complete
    Skip --> Complete

    Complete --> [*]
```

---

## Component Interaction Diagram

```mermaid
graph TB
    subgraph GitHub["GitHub Ecosystem"]
        GH1["GitHub App<br/>(Installation)"]
        GH2["PR Webhook<br/>(Events)"]
    end

    subgraph FastAPI_Layer["Server Layer"]
        FA["FastAPI Server<br/>(Async)"]
        HC["Health Check"]
    end

    subgraph Orchestration["Orchestration Layer"]
        LG["LangGraph<br/>(State Machine)"]
        CP["Checkpoint<br/>(SQLite)"]
    end

    subgraph Services_Layer["External Services"]
        GA["GitHub API<br/>Client"]
        LLM["Google Gemini<br/>LLM"]
        RAG["FAISS RAG<br/>Vector DB"]
        SK["Slack API<br/>Client"]
    end

    subgraph Output["Output Layer"]
        GPC["GitHub<br/>Comments"]
        SLK["Slack<br/>Messages"]
    end

    GH2 -->|HTTP Request| FA
    FA -->|Health| HC
    FA -->|State| LG
    LG -->|Persist| CP

    LG -->|API Call| GA
    LG -->|LLM Request| LLM
    LG -->|Search| RAG
    LG -->|Notify| SK

    GA -->|Fetch/Post| GH1
    SK -->|Message| SLK
    GA -->|Comments| GPC

    style GitHub fill:#e8f4f8,stroke:#333,stroke-width:2px
    style FastAPI_Layer fill:#ffe8cc,stroke:#333,stroke-width:2px
    style Orchestration fill:#e8f8e8,stroke:#333,stroke-width:2px
    style Services_Layer fill:#f0e8f8,stroke:#333,stroke-width:2px
    style Output fill:#ffe8e8,stroke:#333,stroke-width:2px
```

---

## How to Export to PNG

### Option 1: Use Mermaid Live Editor (Free, No Installation)
1. Go to [mermaid.live](https://mermaid.live)
2. Copy the diagram code from this file
3. Paste into the editor
4. Click **Download** → **PNG**

### Option 2: Use mermaid-cli (Local)
```bash
npm install -g @mermaid-js/mermaid-cli
mmdc -i ARCHITECTURE_DIAGRAM.md -o architecture.png
```

### Option 3: Use GitHub's Built-in Rendering
- GitHub automatically renders Mermaid diagrams in markdown
- Right-click → Save image as PNG

---

## Legend

| Color | Component Type |
|-------|-----------------|
| 🔵 Light Blue | External Inputs |
| 🟠 Light Orange | Server/API |
| 🟢 Light Green | Orchestration/Nodes |
| 🟣 Light Purple | External Services |
| 🔴 Light Red | Output/Results |
| ⚫ Gray | Persistence/Checkpoint |

---

**Generated**: 2026-04-13
**Version**: 1.0.0
**Framework**: LangGraph, FastAPI, FAISS RAG, Google Gemini
**Status**: Ready for Publication
