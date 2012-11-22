from init import initialize_sql
from sqlalchemy import create_engine
import sys

from tests import test

def _open(filelike, mode='r'):
    if hasattr(filelike, 'read'): return filelike
    return open(filelike, mode)

class Genome(object):
    url = "mysql://%(user)s%(password)s@%(host)s/%(db)s"
    __tables = {}

    def __init__(self, db="", user="genome", host="genome-mysql.cse.ucsc.edu",
            password="", engine=None):

        if engine is not None:
            self.engine = engine
        else:
            if db.startswith(("sqlite://", "mysql://", "postgresql://")):
                self.url = db
                self.dburl = db
                self.user = self.host = self.password = ""
            else:
                self.db = db
                if user == "genome" and host != "genome-mysql.cse.ucsc.edu":
                    import getpass
                    user = getpass.getuser()
                self.host = host
                self.user = user
                self.password = (":" + password) if password else ""
                self.dburl = self.url % dict(db=self.db, user=self.user,
                    host=self.host, password=self.password)

            self.engine = create_engine(self.dburl)


        self.session, self.Base = initialize_sql(self.engine)
        self.models = __import__("cruzdb.models", globals(), locals(),
                [], -1).models

    def mirror(self, tables, dest_url):
        from mirror import mirror
        return mirror(self, tables, dest_url)

    def dataframe(self, table, limit=None, offset=None):
        from pandas import DataFrame
        if isinstance(table, basestring):
            table = getattr(self, table)
        records = table.table().select()
        if not limit is None:
            records = records.limit(limit)
        if not offset is None:
            records = records.offset(offset)
        records = list(records.execute())
        cols = [c.name for c in table.table().columns]
        return DataFrame.from_records(records, columns=cols)

    def _map(self, table):
        # if the table hasn't been mapped, do so here.
        #if not table in self.__tables:
        if not table in self.Base.metadata.tables:
            # make a new class
            try:
                klass = getattr(self.models, table, None)
                for k in getattr(klass, "__preload_classes__", []):
                    self._map(k)
                self.__tables[table] = type(table, (self.Base, getattr(self.models, table)), {})
            except Exception:
                if klass is not None:
                    raise
                if table.startswith('snp'):
                    self.__tables[table] = type(table, (self.Base, getattr(self.models, 'SNP')), {})
                else:
                    self.__tables[table] = type(table, (self.Base,
                        self.models.Feature), {})
        return self.__tables[table]

    def __getattr__(self, table):
        self._map(table)
        mapped = self.session.query(self.__tables[table])
        mapped.table = lambda : self.table(table)
        mapped.orm = lambda : self._map(table)
        return mapped

    def table(self, table):
        self._map(table)
        return self.Base.metadata.tables[table]

    def bin_query(self, table, chrom, start, end):
        if isinstance(table, basestring):
            table = getattr(self, table)
        tbl = table.table()

        q = table.filter(tbl.c.chrom == chrom)
        if hasattr(tbl.c, "bin"):
            bins = Genome.bins(start, end)
            q = q.filter(tbl.c.bin.in_(bins))

        if hasattr(tbl.c, "txStart"):
            return q.filter(tbl.c.txStart <= end).filter(tbl.c.txEnd >= start)
        return q.filter(tbl.c.chromStart <= end).filter(tbl.c.chromEnd >= start)

    def upstream(self, table, chrom_or_feat, start=None, end=None, k=1):
        res = self.knearest(table, chrom_or_feat, start, end, k, "up")
        end = getattr(chrom_or_feat, "end", end)
        start = getattr(chrom_or_feat, "start", start)
        rev = getattr(chrom_or_feat, "strand", "+") == "-"
        if rev:
            return [x for x in res if x.end > start]
        else:
            return [x for x in res if x.start < end]

    def downstream(self, table, chrom_or_feat, start=None, end=None, k=1):
        res = self.knearest(table, chrom_or_feat, start, end, k, "down")
        end = getattr(chrom_or_feat, "end", end)
        start = getattr(chrom_or_feat, "start", start)
        rev = getattr(chrom_or_feat, "strand", "+") == "-"
        if rev:
            return [x for x in res if x.start < end]
        else:
            return [x for x in res if x.end > start]

    def knearest(self, table, chrom_or_feat, start=None, end=None, k=1,
            _direction=None):
        assert _direction in (None, "up", "down")

        # they sent in a feature
        if start is None:
            assert end is None
            chrom, start, end = chrom_or_feat.chrom, chrom_or_feat.start, chrom_or_feat.end

            # if the query is directional and the feature as a strand,
            # adjust...
            if _direction in ("up", "down") and getattr(chrom_or_feat,
                    "strand", None) == "-":
                _direction = "up" if _direction == "down" else "up"
        else:
            chrom = chrom_or_feat

        qstart, qend = start, end
        res = self.bin_query(table, chrom, qstart, qend)
        i, change = 1, 350
        while res.count() < k:
            if _direction in (None, "up"):
                if qstart == 0 and _direction == "up": break
                qstart = max(0, qstart - change)
            if _direction in (None, "down"):
                qend += change
            i += 1
            change *= (i + 5)
            res = self.bin_query(table, chrom, qstart, qend)
        def dist(f):
            d = 0
            if start > f.end:
                d = start - f.end
            elif f.start > end:
                d = f.start - end
            # add dist as an attribute to the feature
            f.dist = d
            return d

        # sort by dist and ...
        res = sorted(res, key=dist)
        if len(res) == k:
            return res

        if k > len(res): # had to break because of end of chrom
            if k == 0: return []
            k = len(res)

        ndist = res[k - 1].dist
        # include all features that are the same distance as the nth closest
        # feature (accounts for ties).
        while k < len(res) and res[k].dist == ndist:
            k = k + 1
        return res[:k]

    def sql(self, query):
        return self.engine.execute(query)

    def annotate(self, fname, tables):
        from .annotate import annotate
        return annotate(self, fname, tables)

    @staticmethod
    def bins(start, end):
        if end - start < 536870912:
            offsets = [585, 73, 9, 1, 0]
        else:
            raise Exception("not implemented")
            offsets = [4681, 585, 73, 9, 1, 0]
        binFirstShift = 17
        binNextShift = 3

        start = start >> binFirstShift
        end = (end - 1)  >> binFirstShift

        bins = [1]
        for offset in offsets:
            bins.extend(range(offset + start, offset + end + 1))
            start >>= binNextShift
            end >>= binNextShift
        return frozenset(bins)

    def __repr__(self):
        return "%s('%s')" % (self.__class__.__name__,
            self.url % dict(db=self.db, user=self.user, host=self.host,
            password="?" if self.password else ""))

    @classmethod
    def save_bed(cls, query, filename=sys.stdout):
        # write a bed12 file of the query.
        out = _open(filename, 'w')
        for o in query:
            out.write(o.bed() + '\n')


if __name__ == "__main__":
    #g = Genome(db="hg18", host="localhost", user="")
    g = Genome(db="hg18")#, host="localhost", user="")

    print g.cpgIslandExt[12].bed()
    print g.cpgIslandExt[12].bed('length', 'perCpg')

    import sys
    #sys.exit()

    f = g.refGene[19]
    print f.bed12()
    f = g.refGene[19]
    print repr(f), f.cdsStart, f.cdsEnd
    print "exons", f.exons
    print "coding exons", f.coding_exons
    print "cds", f.cds

    print "introns", f.introns
    print "5'utr", f.utr5
    print "3'utr", f.utr3

    print f.browser_link
    #f.txEnd = f.txStart + 30
    #print list(f.blat())
    #print f.cds_sequence
    import time
    from sqlalchemy import and_
    query = g.refGene.filter(and_(g.table('refGene').c.txStart > 10000, g.table('refGene').c.txEnd < 40000))
    t = time.time()
    query.all()
    print time.time() - t

    query = g.refGene.filter(and_(g.table('refGene').c.txStart > 10000, g.table('refGene').c.txEnd < 40000))
    query = query.filter(g.table('refGene').c.bin.in_(Genome.bins(10000,
        40000)))

    t = time.time()
    query = g.bin_query(g.refGene, "chr1", 10000, 40000)

    query.all()
    print time.time() - t
    1/0


    g = Genome('hg19')
    t = time.time()
    q = g.snp135Common
    q = q.filter(q.table.c.bin.in_(Genome.bins(1000, 2000)))
    print q
    q.first()
    print time.time() - t

    #Genome.save_bed(query)
    1/0

    """
    for transcript in g.refGene:
        print transcript, transcript.sequence()[:100] + "..."
        if transcript.txEnd > 8000: break
    """

    kg = g.table('refGene')
    q = kg.select(kg.c.txStart < 5000)

    print list(g.session.execute(q))
