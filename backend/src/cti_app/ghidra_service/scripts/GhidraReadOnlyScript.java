import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;

import java.io.FileInputStream;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;
import java.util.Locale;

/** Fixed read-only operations exposed by the AutoWork Ghidra service contract. */
public class GhidraReadOnlyScript extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] arguments = getScriptArgs();
        if (arguments.length != 2) {
            throw new IllegalArgumentException("The fixed service script expects two paths");
        }
        Properties request = new Properties();
        try (FileInputStream input = new FileInputStream(arguments[0])) {
            request.load(input);
        }
        String operation = decode(request.getProperty("operation", ""));
        String address = decode(request.getProperty("address", ""));
        String query = decode(request.getProperty("query", ""));
        int limit = Integer.parseInt(request.getProperty("limit", "100"));
        String result = switch (operation) {
            case "list_functions" -> listFunctions(limit);
            case "list_imports" -> listImports(limit);
            case "xrefs_to" -> xrefs(address, limit, true);
            case "xrefs_from" -> xrefs(address, limit, false);
            case "decompile_function" -> decompile(address);
            case "search" -> search(query, limit);
            default -> throw new IllegalArgumentException("Operation is not in the fixed catalog");
        };
        Files.writeString(Path.of(arguments[1]), result, StandardCharsets.UTF_8);
    }

    private String listFunctions(int limit) {
        StringBuilder output = new StringBuilder("{\"functions\":[");
        FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
        int count = 0;
        while (functions.hasNext() && count < limit) {
            Function function = functions.next();
            if (count++ > 0) output.append(',');
            output.append("{\"name\":").append(json(function.getName()))
                .append(",\"address\":").append(json(function.getEntryPoint().toString()))
                .append(",\"size\":").append(function.getBody().getNumAddresses()).append('}');
        }
        return output.append("],\"truncated\":").append(functions.hasNext()).append('}').toString();
    }

    private String listImports(int limit) {
        StringBuilder output = new StringBuilder("{\"imports\":[");
        SymbolIterator symbols = currentProgram.getSymbolTable().getAllSymbols(true);
        int count = 0;
        while (symbols.hasNext() && count < limit) {
            Symbol symbol = symbols.next();
            if (!symbol.isExternal()) continue;
            if (count++ > 0) output.append(',');
            output.append("{\"name\":").append(json(symbol.getName()))
                .append(",\"address\":").append(json(symbol.getAddress().toString()))
                .append(",\"library\":").append(json(symbol.getParentNamespace().getName())).append('}');
        }
        return output.append("],\"truncated\":").append(symbols.hasNext()).append('}').toString();
    }

    private String xrefs(String rawAddress, int limit, boolean incoming) {
        Address address = currentProgram.getAddressFactory().getAddress(stripPrefix(rawAddress));
        if (address == null) return "{\"xrefs\":[],\"truncated\":false}";
        ReferenceManager references = currentProgram.getReferenceManager();
        StringBuilder output = new StringBuilder("{\"xrefs\":[");
        int count = 0;
        boolean truncated = false;
        if (incoming) {
            ReferenceIterator iterator = references.getReferencesTo(address);
            while (iterator.hasNext() && count < limit) {
                if (count++ > 0) output.append(',');
                appendReference(output, iterator.next());
            }
            truncated = iterator.hasNext();
        } else {
            Reference[] outgoing = references.getReferencesFrom(address);
            for (int index = 0; index < outgoing.length && count < limit; index++) {
                if (count++ > 0) output.append(',');
                appendReference(output, outgoing[index]);
            }
            truncated = outgoing.length > count;
        }
        return output.append("],\"truncated\":").append(truncated).append('}').toString();
    }

    private String decompile(String rawAddress) throws Exception {
        Address address = currentProgram.getAddressFactory().getAddress(stripPrefix(rawAddress));
        Function function = address == null ? null : currentProgram.getFunctionManager().getFunctionAt(address);
        if (function == null) {
            return "{\"status\":\"unsupported_function\",\"reason\":"
                + json("function_not_found_at_address") + ",\"function\":null}";
        }
        if (function.isExternal() || function.getBody().isEmpty()) {
            return "{\"status\":\"unsupported_function\",\"reason\":"
                + json("function_body_not_available") + ",\"function\":"
                + json(function.getName()) + "}";
        }
        DecompInterface decompiler = new DecompInterface();
        try {
            if (!decompiler.openProgram(currentProgram)) {
                return "{\"status\":\"decompile_failed\",\"reason\":"
                    + json("decompiler_program_open_failed") + ",\"function\":"
                    + json(function.getName()) + "}";
            }
            DecompileResults result = decompiler.decompileFunction(function, 45, monitor);
            if (!result.decompileCompleted() || result.getDecompiledFunction() == null) {
                String reason = result.isTimedOut() ? "decompiler_timed_out" : "decompiler_failed";
                return "{\"status\":\"decompile_failed\",\"reason\":" + json(reason)
                    + ",\"function\":" + json(function.getName()) + "}";
            }
            String code = result.getDecompiledFunction().getC();
            if (code == null || code.isBlank()) {
                return "{\"status\":\"decompile_failed\",\"reason\":"
                    + json("empty_decompilation") + ",\"function\":"
                    + json(function.getName()) + "}";
            }
            if (code.length() > 16000) code = code.substring(0, 16000);
            return "{\"status\":\"succeeded\",\"reason\":null,\"function\":{\"name\":"
                + json(function.getName())
                + ",\"address\":" + json(function.getEntryPoint().toString())
                + ",\"decompilation\":" + json(code) + "}}";
        } finally {
            decompiler.dispose();
        }
    }

    private static void appendReference(StringBuilder output, Reference reference) {
        output.append("{\"from\":").append(json(reference.getFromAddress().toString()))
            .append(",\"to\":").append(json(reference.getToAddress().toString()))
            .append(",\"type\":").append(json(reference.getReferenceType().getName())).append('}');
    }

    private static String stripPrefix(String address) {
        return address.startsWith("0x") || address.startsWith("0X") ? address.substring(2) : address;
    }

    private String search(String query, int limit) {
        String folded = query.toLowerCase(Locale.ROOT);
        StringBuilder output = new StringBuilder("{\"matches\":[");
        SymbolIterator symbols = currentProgram.getSymbolTable().getAllSymbols(true);
        int count = 0;
        while (symbols.hasNext() && count < limit) {
            Symbol symbol = symbols.next();
            String name = symbol.getName();
            if (!name.toLowerCase(Locale.ROOT).contains(folded)) continue;
            if (count++ > 0) output.append(',');
            output.append("{\"name\":").append(json(name))
                .append(",\"address\":").append(json(symbol.getAddress().toString()))
                .append(",\"external\":").append(symbol.isExternal()).append('}');
        }
        return output.append("],\"truncated\":").append(symbols.hasNext()).append('}').toString();
    }

    private static String decode(String value) {
        return URLDecoder.decode(value, StandardCharsets.UTF_8);
    }

    private static String json(String value) {
        StringBuilder output = new StringBuilder("\"");
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"' -> output.append("\\\"");
                case '\\' -> output.append("\\\\");
                case '\n' -> output.append("\\n");
                case '\r' -> output.append("\\r");
                case '\t' -> output.append("\\t");
                default -> {
                    if (character < 0x20) output.append(String.format("\\u%04x", (int) character));
                    else output.append(character);
                }
            }
        }
        return output.append('"').toString();
    }
}
