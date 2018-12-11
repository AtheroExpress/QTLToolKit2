#!/usr/bin/env python

'''
gen eff/oth | gwas eff/oth | eff freq gen/gwas | act

# non ambivalent
A G | A G |  _   _  | nothing
A G | T C |  _   _  | nothing
A G | G A |  _   _  | flip freq, flip beta
A G | C T |  _   _  | flip freq, flip beta
report when not is close!

# ambivalent alleles
## freqs close and low/high
G C | G C | 0.1 0.1 | nothing
G C | C G | 0.1 0.1 | nothing

## freqs inverted and low/high
G C | G C | 0.1 0.9 | flip freq, flip beta
G C | C G | 0.1 0.9 | flip freq, flip beta

## freqs close and mid
G C | C G | 0.5 0.6 | throw away
G C | G C | 0.5 0.6 | throw away
'''

from __future__ import print_function

import os
import argparse
import collections
import gzip
import time

import numpy as np
from pyliftover import LiftOver

GWAS = '/home/llandsmeer/Data/CTMM/cardiogram_gwas_results.txt.gz'
GWAS = '/home/llandsmeer/Data/cad.txt.gz'
STATS = '/home/llandsmeer/Data/CTMM/ctmm_1kGp3GoNL5_RAW_rsync.stats.gz'

parser = argparse.ArgumentParser()
parser.add_argument('-o', '--out', dest='outfile', metavar='cojo',
        type=os.path.abspath, help='Output .cojo file')
parser.add_argument('-r', '--report', dest='report', metavar='txt',
        type=os.path.abspath, help='Report here')
parser.add_argument('-g', '--gen', dest='gen', metavar='file.stats.gz', default=STATS,
        type=os.path.abspath, help='Genetic data', required=False) # TODO
parser.add_argument('--gwas', dest='gwas', metavar='file.txt.gz.', default=GWAS,
        type=os.path.abspath, help='illuminaHumanv4 sqlite database path', required=False) # TODO
filter_parser = parser.add_argument_group('filter snps')
filter_parser.add_argument('--fmid', dest='fmid', metavar='MID',
        help='ambivalent snps are ambiguous when effect frequency is between 0.5-MID and 0.5+MID. ' +
             'set to 0 to prevent discarding. default is 0.05.',
        default='0.1', type=float)
filter_parser.add_argument('--fclose', dest='fclose', metavar='CLOSE',
        help='frequencies are considered close when their difference is less than CLOSE ' +
             'default is 0.1',
        default='0.1', type=float)
header_parser = parser.add_argument_group('gwas header')
header_parser.add_argument('--gwas:effect', dest='effect_allele', metavar='COLUMN',
        help='Effect allele column name')
header_parser.add_argument('--gwas:other', dest='other_allele', metavar='COLUMN',
        help='Non-effect allele column name')
header_parser.add_argument('--gwas:freq', dest='effect_allele_frequency', metavar='COLUMN',
        help='Effect allele frequency column name')
header_parser.add_argument('--gwas:beta', dest='beta', metavar='COLUMN',
        help='Log-odds column name')
header_parser.add_argument('--gwas:std', dest='beta_std', metavar='COLUMN',
        help='Log-odds standard deviation column name')
header_parser.add_argument('--gwas:p', dest='p-value', metavar='COLUMN',
        help='P-value column name')
header_parser.add_argument('--gwas:pos', dest='p-value', metavar='COLUMN',
        help='chr:pos type column name')
header_parser.add_argument('--gwas:chr', dest='p-value', metavar='COLUMN',
        help='chromosome column name')
header_parser.add_argument('--gwas:bp', dest='p-value', metavar='COLUMN',
        help='chromosomal position column name')

# Optimizations, with speeds measured on my laptop
#  680.2klines/s first version
#  802.7klines/s line.split(None, maxsplit)
#                in the hot path, only split until enough fields have
#                been read to read chr and bp
#  871.8klines/s int(bp) -> bp
#                storing basepair position as a string in the gwas dict
#                prevents a call to int(bp) in reading the genetic data
#  914.4klines/s gwas.get -> gwas[pos]
#                first checking if a item is in a dictionary and then
#                retrieving it is faster here than the combined operation
#  969.4klines/s chr 1 -> 01
#                in the gwas dictionary, chrs are stored in the .stats.gz
#                format with leading 0 to prevent a call to str.lstrip
# 1635.6klines/s max IO bound, no useful calculations

if not hasattr(time, 'monotonic'):
    time.monotonic = time.time
if not hasattr(os.path, 'commonpath'):
    os.path.commonpath = os.path.commonprefix

GWASRow = collections.namedtuple('GWASRow', 'ref oth f b se p lineno ch bp')
INV = { 'A': 'T', 'T': 'A', 'C': 'G', 'G': 'C', }
ACT_NOP, ACT_SKIP, ACT_FLIP, ACT_REM, ACT_REPORT_FREQ= 1, 2, 3, 4, 5

def conv_chr_letter(ch):
    # assuming ch = ch.lstrip('0').upper()
    if ch == '23': return 'X'
    elif ch == '24': return 'Y'
    elif ch == '25': return 'XY'
    elif ch == '26': return 'MT'
    elif ch == 'M': return 'MT'
    return ch

class ReporterLine:
    def __init__(self, line=''):
        self.line = line
        self.last_time = time.monotonic()
        self.last_lineno = 0
        self.last_tell = 0
        self.size = None
        print()

    def update(self, lineno, fileno, message=''):
        now = time.monotonic()
        tell = os.lseek(fileno, 0, os.SEEK_CUR)
        if self.size is None:
            self.size = os.fstat(fileno).st_size
        dt = now - self.last_time
        dlineno = lineno - self.last_lineno
        dtell = tell - self.last_tell
        part = 100. * tell / self.size
        kline_per_s = dlineno/dt/1000
        tell_per_s = dtell/dt/1000000
        print('\033[1A\033[K', end='')
        print(self.line, '{0} {1:.1f}kline/s {2:.1f}% {3:.1f}M/s {message}'.format(
                lineno, kline_per_s, part, tell_per_s, message=message))
        self.last_time, self.last_lineno, self.last_tell = now, lineno, tell

def inv(dna):
    if len(dna) == 1:
        return INV[dna]
    return ''.join(INV[bp] for bp in dna)


def select_action(args,
         gen_a, gen_b,
         gen_maj, gen_min,
         gen_maf,
         gwas_ref, gwas_oth,
         gwas_ref_freq):
    # b is the effect allele
    gen_b_freq = gen_maf if gen_b == gen_min else 1 - gen_maf
    freq_close = abs(gen_b_freq - gwas_ref_freq) < args.fclose
    freq_inv_close = abs((1-gen_b_freq) - gwas_ref_freq) < args.fclose
    freq_mid = abs(gen_b_freq - 0.5) < args.fmid
    ambivalent = gen_a == inv(gen_b)
    if not ambivalent:
        if gen_b == gwas_ref and gen_a == gwas_oth:
            act = ACT_NOP
        elif gen_b == gwas_oth and gen_a == gwas_ref:
            act = ACT_FLIP
        elif gen_b == inv(gwas_ref) and gen_a == inv(gwas_oth):
            act = ACT_NOP
        elif gen_b == inv(gwas_oth) and gen_a == inv(gwas_ref):
            act = ACT_FLIP
        else:
            return gen_b_freq, ACT_SKIP
        if act is ACT_NOP and not freq_close:
            return gen_b_freq, ACT_REPORT_FREQ
        if act is ACT_FLIP and not freq_inv_close:
            return gen_b_freq, ACT_REPORT_FREQ
        return gen_b_freq, act
    else:
        if gen_b == gwas_ref and gen_a == gwas_oth:
            equal = True
        elif gen_a == gwas_ref and gen_b == gwas_oth:
            equal = False
        else:
            return gen_b_freq, ACT_SKIP
        if freq_mid:
            return gen_b_freq, ACT_REM
        if freq_close:
            return gen_b_freq, ACT_NOP
        elif freq_inv_close:
            return gen_b_freq, ACT_FLIP
        else:
            return gen_b_freq, ACT_REPORT_FREQ

GWAS_H_POS_COMB_OPTIONS = ['chr_pos_(b36)']
GWAS_H_CHR_OPTIONS = ['chr']
GWAS_H_BP_OPTIONS = ['bp_hg19']
GWAS_H_REF_OPTIONS = ['reference_allele', 'effect_allele']
GWAS_H_OTH_OPTIONS = ['other_allele', 'noneffect_allele']
GWAS_H_FREQ_OPTIONS = ['ref_allele_frequency', 'effect_allele_freq']
GWAS_H_BETA_OPTIONS = ['log_odds', 'logOR']
GWAS_H_SE_OPTIONS = ['log_odds_se', 'se_gc']
GWAS_H_PVALUE_OPTIONS = ['pvalue', 'p-value_gc']
GWAS_HG18_HINTS = ['hg18', 'b36']
GWAS_HG19_HINTS = ['hg19']

def log_error(report, name, gwas, gen=()):
    parts = list(gwas)
    if gen:
        parts.extend(gen)
    print(name, *parts, file=report, sep='\t')


def read_gwas(filename, report=None):
    liftover = None
    yes = no = 0
    desc = {}
    def select(name, options, fail=True):
        if name in desc:
            return header.index(desc[name])
        for option in options:
            if option in header:
                desc[name] = option
                return header.index(option)
        if fail:
            print('could not find a header in GWAS for', name)
            print('specify with --gwas:'+name)
            exit(1)
    try:
        with gzip.open(filename, 'rt') as f:
            for lineno, line in enumerate(f, 1):
                if lineno == 1:
                    if any(hint in line for hint in GWAS_HG19_HINTS):
                        desc['build'] = 'hg19'
                    elif any(hint in line for hint in GWAS_HG18_HINTS):
                        desc['build'] = 'hg18'
                    header = line.split()
                    hpos = select('pos', GWAS_H_POS_COMB_OPTIONS, fail=False)
                    if hpos is None:
                        postype_combined = False
                        hpos_ch = select('chr', GWAS_H_CHR_OPTIONS)
                        hpos_bp = select('bp', GWAS_H_BP_OPTIONS)
                    else:
                        hpos, desc['pos'] = hpos_tuple
                        postype_combined = True
                    href = select('effect', GWAS_H_REF_OPTIONS)
                    hoth = select('other', GWAS_H_OTH_OPTIONS)
                    hfreq = select('freq', GWAS_H_FREQ_OPTIONS)
                    hb = select('beta', GWAS_H_BETA_OPTIONS)
                    hse = select('std', GWAS_H_SE_OPTIONS)
                    hp = select('p', GWAS_H_PVALUE_OPTIONS)
                    if 'build' not in desc:
                        print('could not determine GWAS genome build. use flag --build <BUILD>')
                        exit(1)
                    if desc['build'] != 'hg19':
                        liftover = LiftOver(desc['build'], 'hg19')
                        print('converting', desc['build'], '->', 'hg19')
                    print('= detected headers =')
                    for k, v in desc.items():
                        print(k.ljust(10), v)
                    print('= reading gwas =')
                    reporter = ReporterLine('reading gwas data')
                    continue
                parts = line.split()
                if postype_combined:
                    ch, bp = parts[hpos].split(':', 1)
                else:
                    ch = parts[hpos_ch]
                    bp = parts[hpos_bp]
                row = GWASRow(parts[href].upper(), parts[hoth].upper(),
                        float(parts[hfreq]), float(parts[hb]), parts[hse], parts[hp], lineno, ch, bp)
                ch = ch.upper()
                if ch.startswith('CHR'):
                    ch = ch[3:]
                ch = ch.lstrip('0')
                ch = conv_chr_letter(ch)
                if liftover:
                    conv = liftover.convert_coordinate('chr'+ch, int(bp))
                    if conv:
                        ch, bp, s19, _ = conv[0]
                        bp = str(bp)
                        if ch.startswith('chr'):
                            ch = ch[3:]
                        yes += 1
                    else:
                        no += 1
                        if report:
                            log_error(report, 'gwas_conv_failed', gwas=row)
                        continue
                ch = ch.zfill(2)
                yield (ch, bp), row
                if lineno % 40000 == 0:
                    reporter.update(lineno, f.fileno())
    except KeyboardInterrupt:
        print('aborted reading gwas data at line', lineno)
    if liftover:
        print('successfully hg18->hg19 converted', yes, 'rows')
        print('conversion failed for', no, 'rows (reported as gwas_conv_failed)')

def update_read_stats(gwas, stats_filename, output=None, report=None):
    reporter = ReporterLine('genetic:')
    if output:
        print('SNP A1 A2 freq b se p n', file=output)
    counts = collections.defaultdict(int)
    freq_comp = np.zeros((40000, 2))
    converted = 0
    stopped = False
    try:
        with gzip.open(stats_filename, 'rt') as f:
            for lineno, line in enumerate(f, 1):
                if not gwas:
                    break
                if lineno == 1:
                    header = line.split()
                    rsid = header.index('RSID')
                    ch = header.index('Chr')
                    pos = header.index('BP')
                    a = header.index('A_allele')
                    b = header.index('B_allele')
                    mi = header.index('MinorAllele')
                    ma = header.index('MajorAllele')
                    maf = header.index('MAF')
                    minsplit = max(ch, pos) + 1
                    continue
                parts = line.split(None, minsplit)
                row_pos = parts[ch], parts[pos]
                if row_pos in gwas:
                    gwas_row = gwas[row_pos]
                    parts = line.split()
                    eff = 1 - maf
                    gen_freq, act = select_action(
                            args,
                            parts[a], parts[b],
                            parts[ma], parts[mi],
                            float(parts[maf]),
                            gwas_row.ref, gwas_row.oth,
                            gwas_row.f)
                    freq, beta = gwas_row.f, gwas_row.b
                    if act is ACT_FLIP:
                        counts['flip'] += 1
                        freq = 1-freq
                        beta = -beta
                    elif act is ACT_REM:
                        counts['report:ambiguous_ambivalent'] += 1
                        del gwas[row_pos]
                        if report:
                            log_error(report, 'ambiguous_ambivalent', gwas=gwas_row, gen=parts)
                        continue
                    elif act is ACT_SKIP:
                        counts['report:allele_mismatch'] += 1
                        if report:
                            log_error(report, 'allele_mismatch', gwas=gwas_row, gen=parts)
                        continue
                    elif act is ACT_REPORT_FREQ:
                        counts['report:frequency_mismatch'] += 1
                        if report:
                            log_error(report, 'frequency_mismatch', gwas=gwas_row, gen=parts)
                        continue
                    else:
                        counts['ok'] += 1
                    del gwas[row_pos]
                    converted += 1
                    freq_comp[converted % freq_comp.shape[0]] = freq, gen_freq
                    if output:
                        print(parts[rsid], parts[b], parts[a], freq, beta,
                              gwas_row.se, gwas_row.p, 'NA', file=output)
                if lineno % 100000 == 0:
                    message = '#'+str(converted)
                    if converted > freq_comp.shape[0]:
                        ss_tot = ((freq_comp[:,1]-freq_comp[:,1].mean())**2).sum()
                        ss_res = ((freq_comp[:,1]-freq_comp[:,0])**2).sum()
                        r2 = 1 - ss_res/ss_tot
                        message += ' freq-r2={0:.4f}'.format(r2)
                    reporter.update(lineno, f.fileno(), message)
    except KeyboardInterrupt:
        print('aborted reading genetic data at line', lineno)
        stopped = True
    print('gwas allele conversions:')
    for k, v in counts.items():
        print(' ', '{:6}'.format(v), k)
    print('leftover gwas row count', len(gwas))
    if report:
        if stopped and len(gwas) > 1000:
            print('not writing leftover rows due to early stop')
        else:
            print(file=report)
            print('LEFTOVER GWAS ROWS', file=report)
            for (ch, pos), row in gwas.items():
                print(ch, pos, row, file=report)
            log_error(report, 'leftover', gwas=gwas_row)

def main(args):
    paths = [args.gen, args.gwas]
    output = report = None
    if args.outfile:
        output = open(args.outfile, 'w')
        paths.append(args.report)
    if args.report:
        report = open(args.report, 'w')
        paths.append(args.report)
    root = os.path.commonpath(paths)
    print('root', root)
    print('genetic data', os.path.relpath(args.gen, root))
    print('gwas', os.path.relpath(args.gwas, root))
    if args.outfile:
        print('output', os.path.relpath(args.outfile, root))
    else:
        print('*WARNING* not writing results (-o)')
    if args.report:
        print('output', os.path.relpath(args.report, root))
        with gzip.open(args.gen, 'rt') as f:
            gen_header = f.readline().split()
        log_error(report, 'type', GWASRow._fields, gen_header)
    else:
        print('*WARNING* not writing report (-r)')
    gwas = {}
    for idx, (pos, row) in enumerate(read_gwas(args.gwas, report=report)):
        gwas[pos] = row
    update_read_stats(gwas, args.gen, output=output, report=report)
    if args.outfile:
        output.close()
    if args.report:
        report.close()

if __name__ == '__main__':
    args = parser.parse_args()
    try:
        main(args)
    except KeyboardInterrupt:
        print('aborted')