import struct

class Reader:
    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos
    def read_byte(self):
        b = self.data[self.pos]; self.pos += 1; return b
    def read_bytes(self, n):
        r = self.data[self.pos:self.pos+n]; self.pos += n; return r
    def read_varint(self):
        result = shift = 0
        while True:
            b = self.read_byte()
            result |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
        return result
    def read_zigzag(self):
        n = self.read_varint(); return (n >> 1) ^ -(n & 1)

def thrift_field(r, prev=0):
    byte = r.read_byte()
    if byte == 0: return None, None
    delta = (byte >> 4) & 0x0F; ft = byte & 0x0F
    fid = prev + delta if delta else r.read_zigzag()
    return fid, ft

def thrift_skip(r, ft):
    if ft in (1, 2): pass
    elif ft == 3: r.read_byte()
    elif ft in (4, 5): r.read_varint()
    elif ft == 6: r.read_varint()
    elif ft == 7: r.read_bytes(8)
    elif ft == 8: r.read_bytes(r.read_varint())
    elif ft in (9, 10):
        h = r.read_byte(); et = h & 0xF; sz = (h >> 4) & 0xF
        if sz == 15: sz = r.read_varint()
        for _ in range(sz): thrift_skip(r, et)
    elif ft == 11:
        sz = r.read_varint()
        if sz > 0:
            t = r.read_byte()
            for _ in range(sz):
                thrift_skip(r, (t >> 4) & 0xF); thrift_skip(r, t & 0xF)
    elif ft == 12:
        fid = 0
        while True:
            fid2, ft2 = thrift_field(r, fid)
            if fid2 is None: break
            thrift_skip(r, ft2); fid = fid2

def thrift_val(r, ft):
    if ft in (1, 2): return ft == 1
    if ft == 3: return r.read_byte()
    if ft in (4, 5): return r.read_zigzag()
    if ft == 6: return r.read_zigzag()
    if ft == 7: return struct.unpack('<d', r.read_bytes(8))[0]
    if ft == 8:
        n = r.read_varint(); raw = r.read_bytes(n)
        try: return raw.decode('utf-8')
        except: return raw
    return None

def snappy_decompress(data):
    pos = 0; ulen = shift = 0
    while True:
        b = data[pos]; pos += 1
        ulen |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    out = bytearray()
    while pos < len(data) and len(out) < ulen:
        tag = data[pos]; pos += 1; tt = tag & 0x03
        if tt == 0:
            ll = (tag >> 2) + 1
            if ll == 61: ll = data[pos] + 1; pos += 1
            elif ll == 62: ll = struct.unpack_from('<H', data, pos)[0] + 1; pos += 2
            elif ll == 63: ll = int.from_bytes(data[pos:pos+3], 'little') + 1; pos += 3
            elif ll == 64: ll = struct.unpack_from('<I', data, pos)[0] + 1; pos += 4
            out.extend(data[pos:pos+ll]); pos += ll
        elif tt == 1:
            length = ((tag >> 2) & 0x07) + 4
            offset = ((tag & 0xE0) << 3) | data[pos]; pos += 1
            for _ in range(length): out.append(out[-offset])
        elif tt == 2:
            length = (tag >> 2) + 1
            offset = struct.unpack_from('<H', data, pos)[0]; pos += 2
            for _ in range(length): out.append(out[-offset])
        elif tt == 3:
            length = (tag >> 2) + 1
            offset = struct.unpack_from('<I', data, pos)[0]; pos += 4
            for _ in range(length): out.append(out[-offset])
    return bytes(out)

def parse_page_header(r):
    ph = {}; fid = 0
    while True:
        fid2, ft = thrift_field(r, fid)
        if fid2 is None: break
        if fid2 == 1: ph['type'] = thrift_val(r, ft)
        elif fid2 == 2: ph['uncompressed_size'] = thrift_val(r, ft)
        elif fid2 == 3: ph['compressed_size'] = thrift_val(r, ft)
        elif fid2 == 5:
            dph = {}; dfid = 0
            while True:
                dfid2, dft = thrift_field(r, dfid)
                if dfid2 is None: break
                if dfid2 == 1: dph['num_values'] = thrift_val(r, dft)
                elif dfid2 == 2: dph['encoding'] = thrift_val(r, dft)
                elif dfid2 == 3: dph['def_encoding'] = thrift_val(r, dft)
                elif dfid2 == 4: dph['rep_encoding'] = thrift_val(r, dft)
                else: thrift_skip(r, dft)
                dfid = dfid2
            ph['data_page'] = dph
        elif fid2 == 7:
            dph = {}; dfid = 0
            while True:
                dfid2, dft = thrift_field(r, dfid)
                if dfid2 is None: break
                if dfid2 == 1: dph['num_values'] = thrift_val(r, dft)
                elif dfid2 == 2: dph['encoding'] = thrift_val(r, dft)
                else: thrift_skip(r, dft)
                dfid = dfid2
            ph['dict_page'] = dph
        else: thrift_skip(r, ft)
        fid = fid2
    return ph

def decode_plain_int64(data, count, offset=0):
    return [struct.unpack_from('<q', data, offset + i*8)[0] for i in range(min(count, (len(data)-offset)//8))]

def decode_plain_double(data, count, offset=0):
    return [struct.unpack_from('<d', data, offset + i*8)[0] for i in range(min(count, (len(data)-offset)//8))]

def decode_plain_byte_array(data, count, offset=0):
    vals = []; pos = offset
    for _ in range(count):
        if pos + 4 > len(data): break
        length = struct.unpack_from('<I', data, pos)[0]; pos += 4
        try: vals.append(data[pos:pos+length].decode('utf-8'))
        except: vals.append(data[pos:pos+length].hex())
        pos += length
    return vals

def decode_rle_hybrid(data, num_values, bit_width, offset=0):
    pos = offset; values = []
    while pos < len(data) and len(values) < num_values:
        header_val = 0; shift = 0
        while pos < len(data):
            b = data[pos]; pos += 1
            header_val |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
        if header_val & 1:  # bit-packed
            count = (header_val >> 1) * 8
            if bit_width == 0:
                values.extend([0] * count); continue
            bits = 0; nbits = 0
            for _ in range(min(count, num_values - len(values))):
                while nbits < bit_width and pos < len(data):
                    bits |= data[pos] << nbits; pos += 1; nbits += 8
                values.append(bits & ((1 << bit_width) - 1))
                bits >>= bit_width; nbits -= bit_width
        else:  # RLE
            count = header_val >> 1
            val_bytes = (bit_width + 7) // 8
            val = int.from_bytes(data[pos:pos+val_bytes], 'little') if val_bytes > 0 else 0
            pos += val_bytes
            values.extend([val] * count)
    return values[:num_values]

def decode_delta_binary_packed(data, num_values, offset=0):
    r = Reader(data, offset)
    block_size = r.read_varint()
    miniblocks_per_block = r.read_varint()
    total_count = r.read_varint()
    first_value = r.read_zigzag()
    decoded = [first_value]
    vpb = block_size // miniblocks_per_block if miniblocks_per_block else 1
    remaining = total_count - 1
    while remaining > 0 and r.pos < len(data):
        min_delta = r.read_zigzag()
        bws = [r.read_byte() for _ in range(miniblocks_per_block) if r.pos < len(data)]
        for bw in bws:
            if remaining <= 0: break
            if bw == 0:
                for _ in range(min(vpb, remaining)):
                    decoded.append(decoded[-1] + min_delta); remaining -= 1
            else:
                bits = 0; nbits = 0
                for _ in range(vpb):
                    if remaining <= 0: break
                    while nbits < bw and r.pos < len(data):
                        bits |= data[r.pos] << nbits; r.pos += 1; nbits += 8
                    delta = (bits & ((1 << bw) - 1)) + min_delta
                    bits >>= bw; nbits -= bw
                    decoded.append(decoded[-1] + delta); remaining -= 1
    return decoded[:num_values]

def read_column(file_data, offset, ptype, num_rows, codec=1, is_optional=True):
    r = Reader(file_data, offset)
    dictionary = None
    values = []
    
    while len(values) < num_rows:
        if r.pos >= len(file_data) - 8: break
        ph = parse_page_header(r)
        if not ph or 'compressed_size' not in ph: break
        
        raw_data = r.read_bytes(ph['compressed_size'])
        if codec == 1:
            try: page_data = snappy_decompress(raw_data)
            except: page_data = raw_data
        else:
            page_data = raw_data
        
        if ph.get('type') == 2:  # DICTIONARY_PAGE
            dc = ph.get('dict_page', {}).get('num_values', 0)
            if ptype == 2: dictionary = decode_plain_int64(page_data, dc)
            elif ptype == 5: dictionary = decode_plain_double(page_data, dc)
            elif ptype == 6: dictionary = decode_plain_byte_array(page_data, dc)
        elif ph.get('type') == 0:  # DATA_PAGE
            dp = ph.get('data_page', {})
            nv = dp.get('num_values', num_rows - len(values))
            enc = dp.get('encoding', 0)
            
            data_offset = 0
            def_levels = None
            
            if is_optional:
                # Read definition levels: 4-byte length prefix + RLE data
                if len(page_data) >= 4:
                    def_len = struct.unpack_from('<I', page_data, 0)[0]
                    data_offset = 4 + def_len
                    # bit_width for def levels of max_def_level=1 is 1
                    def_levels = decode_rle_hybrid(page_data[4:4+def_len], nv, 1)
            
            non_null_count = sum(def_levels) if def_levels else nv
            val_data = page_data[data_offset:]
            
            if enc in (2, 8):  # DICTIONARY
                bw = val_data[0] if val_data else 0
                indices = decode_rle_hybrid(val_data[1:], non_null_count, bw)
                if dictionary:
                    non_null_vals = [dictionary[i] if i < len(dictionary) else None for i in indices]
                else:
                    non_null_vals = indices
            elif enc == 0:  # PLAIN
                if ptype == 2: non_null_vals = decode_plain_int64(val_data, non_null_count)
                elif ptype == 5: non_null_vals = decode_plain_double(val_data, non_null_count)
                elif ptype == 6: non_null_vals = decode_plain_byte_array(val_data, non_null_count)
                else: non_null_vals = []
            elif enc == 5:  # DELTA_BINARY_PACKED
                non_null_vals = decode_delta_binary_packed(val_data, non_null_count)
            else:
                non_null_vals = []
            
            # Merge with def levels
            if def_levels:
                vi = 0
                for dl in def_levels:
                    if dl == 1 and vi < len(non_null_vals):
                        values.append(non_null_vals[vi]); vi += 1
                    else:
                        values.append(None)
            else:
                values.extend(non_null_vals)
        else:
            break
    return values[:num_rows]

def parse_file_metadata(data):
    r = Reader(data)
    meta = {'schema': [], 'row_groups': []}; fid = 0
    while True:
        fid2, ft = thrift_field(r, fid)
        if fid2 is None: break
        if fid2 == 1: meta['version'] = thrift_val(r, ft)
        elif fid2 == 2:
            h = r.read_byte(); et = h & 0xF; sz = (h >> 4) & 0xF
            if sz == 15: sz = r.read_varint()
            for _ in range(sz):
                elem = {}; sfid = 0
                while True:
                    sfid2, sft = thrift_field(r, sfid)
                    if sfid2 is None: break
                    if sfid2 == 1: elem['type'] = thrift_val(r, sft)
                    elif sfid2 == 3: elem['repetition'] = thrift_val(r, sft)
                    elif sfid2 == 4:
                        val = thrift_val(r, sft)
                        elem['name'] = val if isinstance(val, str) else val.decode('utf-8','replace') if isinstance(val,(bytes,bytearray)) else str(val)
                    elif sfid2 == 5: elem['num_children'] = thrift_val(r, sft)
                    else: thrift_skip(r, sft)
                    sfid = sfid2
                meta['schema'].append(elem)
        elif fid2 == 3: meta['num_rows'] = thrift_val(r, ft)
        elif fid2 == 4:
            h = r.read_byte(); et = h & 0xF; sz = (h >> 4) & 0xF
            if sz == 15: sz = r.read_varint()
            for _ in range(sz):
                rg = {'columns': []}; rfid = 0
                while True:
                    rfid2, rft = thrift_field(r, rfid)
                    if rfid2 is None: break
                    if rfid2 == 1:
                        h2 = r.read_byte(); et2 = h2 & 0xF; sz2 = (h2 >> 4) & 0xF
                        if sz2 == 15: sz2 = r.read_varint()
                        for _ in range(sz2):
                            cc = {}; cfid = 0
                            while True:
                                cfid2, cft = thrift_field(r, cfid)
                                if cfid2 is None: break
                                if cfid2 == 2: cc['file_offset'] = thrift_val(r, cft)
                                elif cfid2 == 3:
                                    cmd = {}; mfid = 0
                                    while True:
                                        mfid2, mft = thrift_field(r, mfid)
                                        if mfid2 is None: break
                                        if mfid2 == 1: cmd['type'] = thrift_val(r, mft)
                                        elif mfid2 == 2:
                                            h3 = r.read_byte(); et3 = h3 & 0xF; sz3 = (h3 >> 4) & 0xF
                                            if sz3 == 15: sz3 = r.read_varint()
                                            cmd['encodings'] = [thrift_val(r, et3) for _ in range(sz3)]
                                        elif mfid2 == 3:
                                            h3 = r.read_byte(); et3 = h3 & 0xF; sz3 = (h3 >> 4) & 0xF
                                            if sz3 == 15: sz3 = r.read_varint()
                                            paths = []
                                            for _ in range(sz3):
                                                v = thrift_val(r, et3)
                                                paths.append(v if isinstance(v,str) else v.decode('utf-8','replace') if isinstance(v,(bytes,bytearray)) else str(v))
                                            cmd['path'] = paths
                                        elif mfid2 == 4: cmd['codec'] = thrift_val(r, mft)
                                        elif mfid2 == 5: cmd['num_values'] = thrift_val(r, mft)
                                        elif mfid2 == 9: cmd['data_page_offset'] = thrift_val(r, mft)
                                        elif mfid2 == 11: cmd['dict_page_offset'] = thrift_val(r, mft)
                                        else: thrift_skip(r, mft)
                                        mfid = mfid2
                                    cc['meta'] = cmd
                                else: thrift_skip(r, cft)
                                cfid = cfid2
                            rg['columns'].append(cc)
                    elif rfid2 == 3: rg['num_rows'] = thrift_val(r, rft)
                    else: thrift_skip(r, rft)
                    rfid = rfid2
                meta['row_groups'].append(rg)
        else: thrift_skip(r, ft)
        fid = fid2
    return meta

def read_parquet(filepath):
    with open(filepath, 'rb') as f:
        file_data = f.read()
    footer_len = struct.unpack('<I', file_data[-8:-4])[0]
    footer_data = file_data[len(file_data)-8-footer_len:len(file_data)-8]
    meta = parse_file_metadata(footer_data)
    num_rows = meta['num_rows']
    
    # Build schema info (skip root element)
    col_schemas = [s for s in meta['schema'] if 'type' in s]
    
    columns = {}
    for rg in meta['row_groups']:
        for i, cc in enumerate(rg['columns']):
            m = cc.get('meta', {})
            col_name = m.get('path', [f'col{i}'])[0]
            ptype = m.get('type', 0)
            codec = m.get('codec', 0)
            start = m.get('dict_page_offset', m.get('data_page_offset', 0))
            is_opt = col_schemas[i].get('repetition', 0) == 1 if i < len(col_schemas) else True
            try:
                vals = read_column(file_data, start, ptype, num_rows, codec, is_opt)
                columns[col_name] = vals
            except Exception as e:
                columns[col_name] = [f'ERROR:{e}']
    return columns, num_rows, meta

if __name__ == '__main__':
    from datetime import datetime, timezone
    def ns_to_dt(ns):
        if ns is None: return None
        try: return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        except: return ns

    for fn in ['usage_events', 'profile_installation', 'rate_card', 'sim_card_plan_history']:
        fp = f'/home/claude/data-engineering-take-home-main/data/{fn}.parquet'
        cols, nrows, _ = read_parquet(fp)
        print(f"\n{'='*120}")
        print(f"TABLE: {fn} ({nrows} rows)")
        print(f"{'='*120}")
        col_names = list(cols.keys())
        print(' | '.join(f'{c:>20s}' for c in col_names))
        print('-' * (23 * len(col_names)))
        for i in range(nrows):
            row = []
            for c in col_names:
                v = cols[c][i] if i < len(cols[c]) else '???'
                if isinstance(v, int) and abs(v) > 1e15: v = ns_to_dt(v)
                row.append(f'{str(v):>20s}')
            print(' | '.join(row))
