import csv
import io


def to_csv(graph, seed_id, meta=None):
    """Render the graph as a flat CSV for auditing and spreadsheet analysis."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow([
        "Source Name",
        "Source Type",
        "Dependency Kind",
        "Target Name",
        "Target Type",
        "Trust Level",
        "Evidence",
        "Source ID",
        "Target ID"
    ])
    
    for edge in graph.edges:
        source_node = graph.nodes[edge.source]
        target_node = graph.nodes.get(edge.target)
        
        target_name = target_node.name if target_node else edge.target.split("/")[-1]
        target_type = target_node.type if target_node else "unknown"
        target_external = target_node.external if target_node else True
        
        trust_level = "GUESS (LLM)" if edge.origin == "model" else "Verified"
        if target_external:
            trust_level += " (Unverified Target)"
            
        writer.writerow([
            source_node.name,
            source_node.type,
            edge.kind,
            target_name,
            target_type,
            trust_level,
            edge.evidence,
            source_node.id,
            edge.target
        ])
        
    return output.getvalue()
