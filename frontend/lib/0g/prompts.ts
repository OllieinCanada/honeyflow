/* Attribution prompt templates — ported from backend/app/services/llm.py */

export interface RepoAnalysisParams {
  readme: string;
  description: string;
  languages: Record<string, number>;
  file_tree: string;
}

export function buildAnalyzeRepoPrompt(p: RepoAnalysisParams): string {
  return `You are analyzing a GitHub repository.

Description: ${p.description || ""}
Languages: ${JSON.stringify(p.languages || {})}

README (first 4000 chars):
${(p.readme || "(no README)").slice(0, 4000)}

File structure:
${(p.file_tree || "").slice(0, 3000)}

Respond with ONLY a JSON object:
{"purpose": "one-sentence description of what this project does", "tech_stack": ["key", "technologies"], "project_type": "library|application|framework|tool|other"}`;
}

export interface SplitDepsParams {
  purpose: string;
  project_type: string;
  tech_stack: string[];
  source_file_count: number;
  dep_count: number;
}

export function buildSplitDepsPrompt(p: SplitDepsParams): string {
  return `You are evaluating how much of a software project's value comes from its original code versus its dependencies.

Project purpose: ${p.purpose}
Project type: ${p.project_type}
Tech stack: ${JSON.stringify(p.tech_stack)}
Number of source files: ${p.source_file_count}
Number of dependencies: ${p.dep_count}

What fraction of this project's value comes from its original custom code vs its dependencies?

Respond with ONLY a JSON object:
{"direct_fraction": 0.XX, "deps_fraction": 0.XX}
The two values MUST sum to exactly 1.0.`;
}

export interface RankDepsParams {
  purpose: string;
  project_type: string;
  tech_stack: string[];
  deps: { name: string; import_count: number }[];
}

export function buildRankDepsPrompt(p: RankDepsParams): string {
  const depList = p.deps
    .slice(0, 40)
    .map((d) => `  ${d.name}: imported in ${d.import_count} files`)
    .join("\n");

  return `You are ranking dependencies by how critical they are to a project's core functionality.

Project purpose: ${p.purpose}
Tech stack: ${JSON.stringify(p.tech_stack)}
Project type: ${p.project_type}

Dependencies and their import frequency:
${depList}

Rate each dependency from 0.0 to 1.0 on how critical it is to the project's core functionality.
Dev tools, linters, test frameworks, and type stubs should score LOW (0.05 to 0.2).
Core runtime dependencies that the project fundamentally relies on should score HIGH (0.6 to 1.0).

Respond with ONLY a JSON object mapping each dependency name to its score:
{"dep_name": 0.X, ...}`;
}

export interface AnalyzePackageParams {
  description: string;
  keywords: string[];
  dep_names: string[];
  readme: string;
  languages: Record<string, number>;
}

export function buildAnalyzePackagePrompt(p: AnalyzePackageParams): string {
  const readmeSec = p.readme
    ? `\nREADME (first 4000 chars):\n${p.readme.slice(0, 4000)}`
    : "";

  return `You are analyzing a software package from a package registry.

Description: ${p.description || "(no description)"}
Keywords: ${JSON.stringify((p.keywords || []).slice(0, 20))}
Languages: ${JSON.stringify(p.languages || {})}
Dependencies: ${JSON.stringify((p.dep_names || []).slice(0, 30))}
${readmeSec}

Respond with ONLY a JSON object:
{"purpose": "one-sentence description of what this package does", "tech_stack": ["key", "technologies"], "project_type": "library|application|framework|tool|other"}`;
}

export interface CitationInfluenceParams {
  paper_title: string;
  paper_abstract: string;
  categories: string[];
  citations: {
    key: string;
    title: string;
    authors: string[];
    year: string | number;
    frequency: number;
    explicit_count?: number;
    conceptual_count?: number;
    contexts?: string[];
  }[];
}

export function buildCitationInfluencePrompt(
  p: CitationInfluenceParams
): string {
  const citList = p.citations
    .slice(0, 30)
    .map((c) => {
      const authors = (c.authors || []).slice(0, 3).join(", ") || "unknown";
      let line = `- [${c.key}] "${(c.title || "untitled").slice(0, 100)}" by ${authors} (${c.year}). Cited ${c.frequency || 0} times.`;
      if (c.explicit_count || c.conceptual_count) {
        line += ` Explicit mentions: ${c.explicit_count || 0}. Conceptual mentions: ${c.conceptual_count || 0}.`;
      }
      if (c.contexts?.length) {
        line += ` Contexts: ${c.contexts
          .slice(0, 2)
          .map((s) => `"${s.slice(0, 150)}"`)
          .join(" | ")}`;
      }
      return line;
    })
    .join("\n");

  return `You are analyzing a research paper's citations to determine its intellectual lineage.
Your goal is to identify which cited works this paper most fundamentally BUILDS UPON or EXTENDS —
not just which papers are mentioned most often.

A paper cited once as "We extend the architecture of [X]" is MORE influential than
a utility paper cited 20 times as "we use the optimizer from [Y]".

Paper being analyzed:
  Title: ${p.paper_title}
  Abstract: ${(p.paper_abstract || "").slice(0, 1500)}
  Categories: ${(p.categories || []).slice(0, 5).join(", ")}

Citations:
${citList}

Rate each citation from 0.0 to 1.0 on how foundational it is to this paper's CORE CONTRIBUTION.

Scoring guide:
  0.8-1.0: Foundational work this paper directly extends or builds upon
  0.5-0.7: Significant methodological or theoretical influence
  0.2-0.4: Useful comparison, baseline, or supporting technique
  0.0-0.1: Incidental mention, dataset source, or general reference

Respond with ONLY a JSON object:
{"cite_key": score, ...}`;
}

export interface AnalyzePaperParams {
  title: string;
  abstract: string;
  categories: string[];
}

export function buildAnalyzePaperPrompt(p: AnalyzePaperParams): string {
  return `You are analyzing a research paper.

Title: ${p.title}
Abstract: ${(p.abstract || "").slice(0, 2000)}
Categories: ${(p.categories || []).slice(0, 5).join(", ")}

Respond with ONLY a JSON object:
{"contribution": "one-sentence description of the paper's main contribution", "research_area": "broad research area (e.g. 'machine learning', 'cryptography')", "paper_type": "theoretical|empirical|survey|system|benchmark|other"}`;
}

export type InferenceAction =
  | "analyze_repo"
  | "split_direct_vs_deps"
  | "rank_dependency_importance"
  | "analyze_package"
  | "rank_citation_influence"
  | "analyze_paper";

const PROMPT_TEMPLATE_VERSION = "honeyflow-attribution-prompts-v1";

export function promptTemplateId(action: InferenceAction): string {
  return `${PROMPT_TEMPLATE_VERSION}/${action}`;
}

export function buildPrompt(
  action: InferenceAction,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params: Record<string, any>
): string {
  switch (action) {
    case "analyze_repo":
      return buildAnalyzeRepoPrompt(params as RepoAnalysisParams);
    case "split_direct_vs_deps":
      return buildSplitDepsPrompt(params as SplitDepsParams);
    case "rank_dependency_importance":
      return buildRankDepsPrompt(params as RankDepsParams);
    case "analyze_package":
      return buildAnalyzePackagePrompt(params as AnalyzePackageParams);
    case "rank_citation_influence":
      return buildCitationInfluencePrompt(params as CitationInfluenceParams);
    case "analyze_paper":
      return buildAnalyzePaperPrompt(params as AnalyzePaperParams);
    default:
      throw new Error(`Unknown action: ${action}`);
  }
}
